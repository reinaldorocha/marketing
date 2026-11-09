"""Transport adapter for Evolution GO (unofficial WhatsApp).

Connects to a running Evolution GO instance via its REST API:
- Instance creation and lifecycle (/instance/create, /instance/connect, /instance/qr, /instance/status, /instance/disconnect)
- Sending text (/send/text) and media (/send/media)
- Ingestion of webhook events (messages, connection status, receipts)
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from django.utils import timezone

from apps.channels.capabilities import capabilities_for
from apps.channels.downgrade import downgrade
from apps.channels.events import (
    EventPayload,
    EventType,
    MediaBlock,
    NormalizedEvent,
    OutboundMessage,
    SendResult,
    SendStatus,
    TextBlock,
)
from apps.channels.models import ChannelConnection, ConnectionStatus, EvolutionSession
from apps.channels.providers.base import Adapter, request_json
from apps.channels.providers.exceptions import APIError
from apps.common.platforms import Platform

logger = logging.getLogger(__name__)

_CAPABILITIES = capabilities_for(Platform.WHATSAPP_EVOLUTION)


def _clean_phone(raw: str) -> str:
    """Strip @s.whatsapp.net, + and non-digits to get the WhatsApp destination number."""
    if not raw:
        return ""
    val = raw.split("@")[0].split(":")[0]
    return re.sub(r"\D", "", val)


def _is_supported_recipient(recipient: str) -> bool:
    cleaned = _clean_phone(recipient)
    return 5 <= len(cleaned) <= 20


class EvolutionClient:
    """HTTP client communicating with the Evolution GO REST API."""

    def __init__(self, api_url: str, api_key: str, instance_id: str = ""):
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key
        self.instance_id = instance_id

    def _headers(self) -> dict[str, str]:
        headers = {
            "apikey": self.api_key,
            "Content-Type": "application/json",
        }
        if self.instance_id:
            headers["instanceId"] = self.instance_id
        return headers

    def create_instance(
        self,
        instance_id: str,
        name: str,
        token: str,
        advanced_settings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Call POST /instance/create."""
        payload: dict[str, Any] = {
            "instanceId": instance_id,
            "name": name,
            "token": token,
            "advancedSettings": advanced_settings
            or {
                "alwaysOnline": False,
                "ignoreGroups": True,
                "ignoreStatus": True,
                "readMessages": False,
                "rejectCall": False,
            },
        }
        return request_json(
            "POST",
            f"{self.api_url}/instance/create",
            headers=self._headers(),
            json=payload,
            timeout=15.0,
        )

    def connect_instance(
        self,
        webhook_url: str,
        subscribe: list[str] | None = None,
        immediate: bool = True,
    ) -> dict[str, Any]:
        """Call POST /instance/connect to trigger socket connect and bind webhook."""
        payload: dict[str, Any] = {
            "webhookUrl": webhook_url,
            "subscribe": subscribe
            or [
                "MESSAGE",
                "READ_RECEIPT",
                "CONNECTION_UPDATE",
                "QRCODE_UPDATED",
            ],
            "immediate": immediate,
        }
        return request_json(
            "POST",
            f"{self.api_url}/instance/connect",
            headers=self._headers(),
            json=payload,
            timeout=15.0,
        )

    def get_qr(self) -> dict[str, Any]:
        """Call GET /instance/qr."""
        return request_json(
            "GET",
            f"{self.api_url}/instance/qr",
            headers=self._headers(),
            timeout=10.0,
        )

    def get_status(self) -> dict[str, Any]:
        """Call GET /instance/status."""
        return request_json(
            "GET",
            f"{self.api_url}/instance/status",
            headers=self._headers(),
            timeout=10.0,
        )

    def disconnect(self) -> dict[str, Any]:
        """Call POST /instance/disconnect."""
        return request_json(
            "POST",
            f"{self.api_url}/instance/disconnect",
            headers=self._headers(),
            timeout=10.0,
        )

    def send_text(
        self,
        number: str,
        text: str,
        quoted: dict[str, Any] | None = None,
        delay: int | None = None,
    ) -> dict[str, Any]:
        """Call POST /send/text."""
        payload: dict[str, Any] = {
            "number": _clean_phone(number),
            "text": text,
        }
        if quoted:
            payload["quoted"] = quoted
        if delay is not None:
            payload["delay"] = delay
        return request_json(
            "POST",
            f"{self.api_url}/send/text",
            headers=self._headers(),
            json=payload,
            timeout=15.0,
        )

    def send_media(
        self,
        number: str,
        media_type: str,
        url: str,
        caption: str = "",
        filename: str = "",
        delay: int | None = None,
    ) -> dict[str, Any]:
        """Call POST /send/media."""
        # Evolution GO accepts: image, video, audio, document
        clean_type = media_type.lower()
        if clean_type == "file":
            clean_type = "document"
        payload: dict[str, Any] = {
            "number": _clean_phone(number),
            "type": clean_type,
            "url": url,
            "caption": caption,
            "filename": filename or "",
        }
        if delay is not None:
            payload["delay"] = delay
        return request_json(
            "POST",
            f"{self.api_url}/send/media",
            headers=self._headers(),
            json=payload,
            timeout=25.0,
        )


class WhatsAppEvolutionAdapter(Adapter):
    """Adapter integrating WhatsApp via Evolution GO."""

    platform = Platform.WHATSAPP_EVOLUTION.value
    capabilities = _CAPABILITIES
    webhook_content = "json"

    def get_client(self, connection: ChannelConnection) -> EvolutionClient:
        creds = connection.credentials or {}
        api_url = creds.get("api_url") or ""
        # Prefer instance token, fallback to global_api_key
        api_key = creds.get("instance_token") or creds.get("api_key") or ""
        instance_id = creds.get("instance_id") or connection.external_id or ""
        if not api_url or not api_key:
            session = EvolutionSession.objects.filter(connection=connection).first()
            if session:
                api_url = api_url or session.api_url
                instance_id = instance_id or session.instance_id
        return EvolutionClient(api_url=api_url, api_key=api_key, instance_id=instance_id)

    def resolve_connection(self, request: Any, raw_body: bytes) -> ChannelConnection | None:
        """Resolve which ChannelConnection owns an inbound Evolution GO delivery."""
        import json

        # 1. Direct connection ID in URL kwargs (e.g. from dedicated route)
        if hasattr(request, "resolver_match") and request.resolver_match:
            conn_id = request.resolver_match.kwargs.get("connection_id")
            if conn_id:
                return (
                    ChannelConnection.objects.unscoped()
                    .filter(pk=conn_id, platform=self.platform)
                    .first()
                )

        # 2. Check header or query param secret
        secret = request.headers.get("X-Webhook-Secret") or request.GET.get("secret")
        if secret:
            conn = ChannelConnection.resolve_by_webhook_secret(secret)
            if conn and conn.platform == self.platform:
                return conn

        # 3. Check payload for instance identifier
        try:
            payload = json.loads(raw_body)
        except Exception:
            return None

        instance_id = (
            payload.get("instance")
            or payload.get("instanceId")
            or payload.get("data", {}).get("instance")
            or payload.get("data", {}).get("instanceId")
        )
        if instance_id:
            # Match by external_id or session instance_id
            conn = (
                ChannelConnection.objects.unscoped()
                .filter(platform=self.platform, external_id=instance_id)
                .first()
            )
            if conn:
                return conn
            session = (
                EvolutionSession.objects.filter(instance_id=instance_id)
                .select_related("connection")
                .first()
            )
            if session and session.connection.platform == self.platform:
                return session.connection

        # 4. Check apikey header matching instance token
        req_key = request.headers.get("apikey") or request.headers.get("x-api-key")
        if req_key:
            for c in ChannelConnection.objects.unscoped().filter(platform=self.platform):
                tok = c.credentials.get("instance_token") or c.credentials.get("api_key")
                if tok and tok == req_key:
                    return c

        return None

    def verify_webhook(self, request: Any, connection: ChannelConnection) -> bool:
        """Validate that the webhook callback is authentic."""
        if not connection or connection.platform != self.platform:
            return False

        # If webhook_secret is set on the connection, verify it if presented
        secret = request.headers.get("X-Webhook-Secret") or request.GET.get("secret")
        if secret and connection.webhook_secret_digest:
            return connection.verify_webhook_secret(secret)

        # Also accept matching apikey header
        req_key = request.headers.get("apikey") or request.headers.get("x-api-key")
        if req_key:
            expected = connection.credentials.get("instance_token") or connection.credentials.get("api_key")
            if expected and expected == req_key:
                return True

        # If connection is active and resolved properly, allow ingestion
        return connection.status != ConnectionStatus.DISABLED

    def parse_events(self, request: Any, connection: ChannelConnection) -> list[NormalizedEvent]:
        """Convert Evolution GO webhook events into NormalizedEvent objects."""
        import json

        try:
            payload = json.loads(request.body)
        except Exception:
            return []

        event_name = (payload.get("event") or "").lower()
        data = payload.get("data", {})

        # 1. Update Connection Status
        if event_name in {"connection.update", "status.instance"}:
            state = (data.get("state") or data.get("status") or "").lower()
            session = EvolutionSession.objects.filter(connection=connection).first()
            if session and state:
                session.state = state
                if state in {"open", "connected"}:
                    session.last_connected_at = timezone.now()
                    if connection.status != ConnectionStatus.ACTIVE:
                        connection.status = ConnectionStatus.ACTIVE
                        connection.save(update_fields=["status"])
                elif state in {"close", "closed", "disconnected"}:
                    session.last_disconnected_at = timezone.now()
                session.save(update_fields=["state", "last_connected_at", "last_disconnected_at", "updated_at"])
            return []

        # 2. Update QR code if pushed via webhook
        if event_name in {"qrcode.updated", "qrcode"}:
            qr = data.get("qrcode") or data.get("base64") or ""
            session = EvolutionSession.objects.filter(connection=connection).first()
            if session and qr:
                session.qr_code_base64 = qr
                session.state = "qrcode"
                session.save(update_fields=["qr_code_base64", "state", "updated_at"])
            return []

        # 3. Message Receipt / Status Update
        if event_name in {"messages.update", "message.receipt", "read_receipt"}:
            key = data.get("key", {})
            msg_id = key.get("id") or data.get("id") or ""
            status_val = (data.get("status") or data.get("receipt") or "").lower()
            if msg_id and status_val:
                mapped_status = "read" if "read" in status_val or "play" in status_val else "delivered"
                return [
                    NormalizedEvent(
                        type=EventType.DELIVERY_STATUS,
                        connection=connection,
                        platform_user_id=_clean_phone(key.get("remoteJid", "")),
                        provider_event_id=f"receipt:{msg_id}:{mapped_status}",
                        timestamp=timezone.now(),
                        payload=EventPayload(
                            extra={
                                "provider_message_id": msg_id,
                                "status": mapped_status,
                            }
                        ),
                        raw=payload,
                    )
                ]
            return []

        # 4. Inbound Message Handling
        if event_name in {"message", "messages.upsert", "messages.create"}:
            key = data.get("key", {})
            if key.get("fromMe", False):
                # Outbound message originated on user's own device; do not duplicate
                return []

            remote_jid = key.get("remoteJid", "")
            if not remote_jid or "@g.us" in remote_jid or "@broadcast" in remote_jid:
                # Groups and status broadcasts are ignored
                return []

            msg_id = key.get("id") or ""
            if not msg_id:
                return []

            phone = _clean_phone(remote_jid)
            push_name = data.get("pushName") or data.get("senderName") or ""
            msg_content = data.get("message") or {}

            # Extract text
            text = (
                msg_content.get("conversation")
                or msg_content.get("extendedTextMessage", {}).get("text")
                or ""
            )

            # Check for media attachments
            attachments: list[str] = []
            caption = ""
            for m_key in ("imageMessage", "videoMessage", "audioMessage", "documentMessage"):
                if m_key in msg_content:
                    media_info = msg_content[m_key]
                    media_url = media_info.get("url") or ""
                    caption = media_info.get("caption") or ""
                    if media_url:
                        attachments.append(media_url)
                    break

            if not text and caption:
                text = caption

            # Timestamp
            ts = timezone.now()
            raw_ts = data.get("messageTimestamp")
            if raw_ts:
                try:
                    ts = datetime.fromtimestamp(int(raw_ts), tz=timezone.utc)
                except Exception:
                    pass

            return [
                NormalizedEvent(
                    type=EventType.MESSAGE,
                    connection=connection,
                    platform_user_id=remote_jid,
                    provider_event_id=msg_id,
                    timestamp=ts,
                    payload=EventPayload(
                        text=text,
                        attachments=tuple(attachments),
                        extra={
                            "sender_name": push_name,
                            "phone": phone,
                            "remote_jid": remote_jid,
                        },
                    ),
                    raw=payload,
                )
            ]

        return []

    def send(
        self,
        connection: ChannelConnection,
        identity: Any,
        outbound: OutboundMessage,
    ) -> SendResult:
        """Send an outbound message through Evolution GO."""
        if connection.platform != self.platform or connection.status != ConnectionStatus.ACTIVE:
            raise APIError("This channel is not available for sending.", status_code=409, code="channel_unavailable")

        recipient_raw = str(getattr(identity, "platform_user_id", ""))
        number = _clean_phone(recipient_raw)
        if not _is_supported_recipient(number):
            raise APIError(
                f"Invalid or unsupported WhatsApp recipient: {recipient_raw}",
                status_code=400,
                code="recipient_unsupported",
            )

        client = self.get_client(connection)
        rendered = downgrade(outbound, self.capabilities)

        last_provider_id = ""
        for message in rendered.messages:
            body = message.to_body()
            blocks = body.get("blocks", [])

            # Handle media blocks
            media_blocks = [b for b in blocks if b.get("type") in ("image", "video", "audio", "file")]
            if media_blocks:
                for block in media_blocks:
                    media_url = block.get("url", "")
                    if not media_url:
                        continue
                    m_kind = block.get("type", "image")
                    caption = block.get("caption") or block.get("text") or ""
                    filename = block.get("filename") or ""
                    try:
                        res = client.send_media(
                            number=number,
                            media_type=m_kind,
                            url=media_url,
                            caption=caption,
                            filename=filename,
                        )
                        last_provider_id = res.get("key", {}).get("id") or res.get("id") or last_provider_id
                    except APIError as exc:
                        logger.error("Evolution GO send_media failed for connection %s: %s", connection.pk, exc)
                        return SendResult(SendStatus.FAILED, error=exc.code or "send_failed")

            # Handle text blocks
            text_blocks = [b for b in blocks if b.get("type") == "text"]
            if text_blocks:
                full_text = "\n\n".join(b.get("text", "") for b in text_blocks if b.get("text"))
                if full_text:
                    try:
                        res = client.send_text(number=number, text=full_text)
                        last_provider_id = res.get("key", {}).get("id") or res.get("id") or last_provider_id
                    except APIError as exc:
                        logger.error("Evolution GO send_text failed for connection %s: %s", connection.pk, exc)
                        return SendResult(SendStatus.FAILED, error=exc.code or "send_failed")

        if not last_provider_id:
            last_provider_id = f"evo:{int(timezone.now().timestamp())}"

        return SendResult(SendStatus.SENT, provider_message_id=last_provider_id[:200])

    def on_disconnect(self, connection: ChannelConnection) -> None:
        """Called when connection is disconnected or disabled."""
        try:
            client = self.get_client(connection)
            client.disconnect()
        except Exception:
            logger.warning("Could not disconnect Evolution GO instance for connection %s", connection.pk)


from apps.channels.registry import register_adapter  # noqa: E402

register_adapter(Platform.WHATSAPP_EVOLUTION, WhatsAppEvolutionAdapter)
