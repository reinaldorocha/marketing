"""Transport adapter for Evolution GO (unofficial WhatsApp).

Connects to a running Evolution GO instance via its REST API:
- Instance creation and lifecycle (/instance/create, /instance/connect, /instance/qr, /instance/status, /instance/disconnect)
- Sending text (/send/text) and media (/send/media)
- Ingestion of webhook events (messages, connection status, receipts)
"""
from __future__ import annotations

import logging
import re
import secrets
from datetime import datetime, timezone as dt_timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx
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
from apps.channels.providers.base import Adapter
from apps.channels.providers.exceptions import APIError, RateLimitError
from apps.common.outbound import BlockedURLError, OutboundError, OutboundTransportError, guarded_request
from apps.common.platforms import Platform

logger = logging.getLogger(__name__)

_CAPABILITIES = capabilities_for(Platform.WHATSAPP_EVOLUTION)


def _clean_recipient(raw: str) -> str:
    """Format recipient for WhatsApp. Keeps group JIDs (@g.us) intact, or strips phone to digits."""
    if not raw:
        return ""
    raw = raw.strip()
    if "@g.us" in raw:
        # It's a group JID: e.g. 120363024849201948@g.us
        return raw.split(":")[0] if ":" in raw else raw
    # It's an individual phone number
    val = raw.split("@")[0].split(":")[0]
    return re.sub(r"\D", "", val)


def _clean_phone(raw: str) -> str:
    """Strip @s.whatsapp.net, + and non-digits to get the WhatsApp destination number."""
    return _clean_recipient(raw)


def _is_supported_recipient(recipient: str) -> bool:
    if recipient and "@g.us" in recipient:
        return True
    cleaned = _clean_phone(recipient)
    return 5 <= len(cleaned) <= 20


def guarded_evolution_request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    json: Any = None,
    params: dict[str, Any] | None = None,
    timeout: float = 15.0,
    client: httpx.Client | None = None,
) -> Any:
    """Execute an outbound HTTP request to the Evolution API via the SSRF guard."""
    if params:
        parsed = urlparse(url)
        existing_q = parse_qsl(parsed.query)
        new_q = [(k, str(v)) for k, v in params.items()]
        new_query = urlencode(existing_q + new_q)
        url = urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, parsed.fragment))

    # All calls to configurable Evolution instance URL go through the SSRF guard (SECURITY-BASELINE §6)
    try:
        response = guarded_request(
            method,
            url,
            headers=headers,
            json=json,
            timeout=timeout,
            client=client,
        )
    except BlockedURLError:
        raise
    except OutboundTransportError as exc:
        raise APIError(f"{method} {url} failed: {exc}") from exc
    except OutboundError as exc:
        raise APIError(f"{method} {url} outbound error: {exc}") from exc

    if response.status_code == 429:
        raise RateLimitError(
            "Evolution API is rate limiting this connection",
            retry_after=None,
            code="rate_limit",
        )
    if not response.ok:
        raise APIError(
            f"{method} Evolution API was rejected",
            status_code=response.status_code,
        )

    try:
        data = response.json()
    except (ValueError, Exception) as exc:
        raise APIError("Evolution API returned a non-JSON body", status_code=response.status_code) from exc

    return data


class EvolutionClient:
    """HTTP client communicating with the Evolution GO REST API."""

    def __init__(
        self,
        api_url: str,
        api_key: str,
        instance_id: str = "",
        client: httpx.Client | None = None,
    ):
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key
        self.instance_id = instance_id
        self.client = client

    def _headers(self, idempotency_key: str = "") -> dict[str, str]:
        headers = {
            "apikey": self.api_key,
            "Content-Type": "application/json",
        }
        if self.instance_id:
            headers["instanceId"] = self.instance_id
        if idempotency_key:
            headers["X-Idempotency-Key"] = idempotency_key
            headers["Idempotency-Key"] = idempotency_key
        return headers

    def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json: Any = None,
        params: dict[str, Any] | None = None,
        timeout: float = 15.0,
    ) -> Any:
        return guarded_evolution_request(
            method,
            url,
            headers=headers if headers is not None else self._headers(),
            json=json,
            params=params,
            timeout=timeout,
            client=self.client,
        )

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
        return self._request(
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
        return self._request(
            "POST",
            f"{self.api_url}/instance/connect",
            headers=self._headers(),
            json=payload,
            timeout=15.0,
        )

    def get_qr(self) -> dict[str, Any]:
        """Call GET /instance/qr."""
        return self._request(
            "GET",
            f"{self.api_url}/instance/qr",
            headers=self._headers(),
            timeout=10.0,
        )

    def get_status(self) -> dict[str, Any]:
        """Call GET /instance/status."""
        return self._request(
            "GET",
            f"{self.api_url}/instance/status",
            headers=self._headers(),
            timeout=10.0,
        )

    def disconnect(self) -> dict[str, Any]:
        """Call POST /instance/disconnect."""
        return self._request(
            "POST",
            f"{self.api_url}/instance/disconnect",
            headers=self._headers(),
            timeout=10.0,
        )

    def find_sent_message(self, idempotency_key: str, recipient: str = "") -> str | None:
        """Query Evolution API to check if a message with this idempotency key was already sent."""
        if not idempotency_key:
            return None
        payload: dict[str, Any] = {
            "where": {
                "idempotencyKey": idempotency_key,
            },
            "idempotencyKey": idempotency_key,
            "idempotency_key": idempotency_key,
        }
        if recipient:
            clean_recip = _clean_recipient(recipient)
            payload["where"]["key"] = {"remoteJid": clean_recip}
            payload["where"]["remoteJid"] = clean_recip

        endpoints = [f"{self.api_url}/chat/findMessages"]
        if self.instance_id:
            endpoints.append(f"{self.api_url}/chat/findMessages/{self.instance_id}")

        for endpoint in endpoints:
            try:
                res = self._request(
                    "POST",
                    endpoint,
                    headers=self._headers(idempotency_key=idempotency_key),
                    json=payload,
                    timeout=10.0,
                )
                if isinstance(res, dict):
                    msg_id = res.get("id") or res.get("messageId") or res.get("key", {}).get("id")
                    if msg_id:
                        return str(msg_id)
                    msgs = res.get("messages") or res.get("records") or res.get("data")
                    if isinstance(msgs, list) and msgs:
                        first = msgs[0]
                        if isinstance(first, dict):
                            return str(
                                first.get("id")
                                or first.get("messageId")
                                or first.get("key", {}).get("id")
                                or "found"
                            )
                elif isinstance(res, list) and res:
                    first = res[0]
                    if isinstance(first, dict):
                        return str(
                            first.get("id")
                            or first.get("messageId")
                            or first.get("key", {}).get("id")
                            or "found"
                        )
            except Exception:
                continue
        return None

    def send_text(
        self,
        number: str,
        text: str,
        quoted: dict[str, Any] | None = None,
        delay: int | None = None,
        mentions: list[str] | None = None,
        idempotency_key: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Call POST /send/text."""
        payload: dict[str, Any] = {
            "number": _clean_recipient(number),
            "text": text,
        }
        if quoted:
            payload["quoted"] = quoted
        if delay is not None:
            payload["delay"] = delay
        if mentions:
            payload["mentions"] = [_clean_recipient(m) for m in mentions if m]
        if idempotency_key:
            payload["idempotency_key"] = idempotency_key
            payload["idempotencyKey"] = idempotency_key
        return self._request(
            "POST",
            f"{self.api_url}/send/text",
            headers=self._headers(idempotency_key=idempotency_key),
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
        mentions: list[str] | None = None,
        ptt: bool = False,
        idempotency_key: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Call POST /send/media with native PTT (Push-To-Talk voice note) support."""
        clean_type = media_type.lower()
        if clean_type == "file":
            clean_type = "document"
        elif clean_type == "ptt":
            clean_type = "audio"
            ptt = True
        payload: dict[str, Any] = {
            "number": _clean_recipient(number),
            "type": clean_type,
            "url": url,
            "caption": caption,
            "filename": filename or "",
        }
        if ptt:
            payload["ptt"] = True
        if delay is not None:
            payload["delay"] = delay
        if mentions:
            payload["mentions"] = [_clean_recipient(m) for m in mentions if m]
        if idempotency_key:
            payload["idempotency_key"] = idempotency_key
            payload["idempotencyKey"] = idempotency_key
        return self._request(
            "POST",
            f"{self.api_url}/send/media",
            headers=self._headers(idempotency_key=idempotency_key),
            json=payload,
            timeout=25.0,
        )

    def send_voice_note(
        self,
        number: str,
        url: str,
        delay: int | None = None,
        mentions: list[str] | None = None,
        idempotency_key: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Send a WhatsApp Push-To-Talk (PTT) voice note (gravado na hora)."""
        return self.send_media(
            number=number,
            media_type="audio",
            url=url,
            delay=delay,
            mentions=mentions,
            ptt=True,
            idempotency_key=idempotency_key,
            **kwargs,
        )

    # -----------------------------------------------------------------------
    # Formatos Especiais da Evolution GO (Item 3)
    # -----------------------------------------------------------------------

    def send_sticker(
        self,
        number: str,
        url: str,
        idempotency_key: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Call POST /send/sticker (WebP image/sticker)."""
        payload: dict[str, Any] = {
            "number": _clean_recipient(number),
            "sticker": url,
        }
        if idempotency_key:
            payload["idempotency_key"] = idempotency_key
            payload["idempotencyKey"] = idempotency_key
        return self._request(
            "POST",
            f"{self.api_url}/send/sticker",
            headers=self._headers(idempotency_key=idempotency_key),
            json=payload,
            timeout=25.0,
        )

    def send_poll(
        self,
        number: str,
        question: str,
        options: list[str],
        max_answers: int = 1,
        idempotency_key: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Call POST /send/poll (Interactive Poll/Enquete)."""
        clean_options = [str(opt).strip() for opt in options if str(opt).strip()]
        if len(clean_options) < 2:
            raise ValueError("Enquete precisa de ao menos 2 opções de resposta.")
        payload: dict[str, Any] = {
            "number": _clean_recipient(number),
            "question": question[:255],
            "options": clean_options[:12],
            "maxAnswer": max(1, min(len(clean_options), max_answers)),
        }
        if idempotency_key:
            payload["idempotency_key"] = idempotency_key
            payload["idempotencyKey"] = idempotency_key
        return self._request(
            "POST",
            f"{self.api_url}/send/poll",
            headers=self._headers(idempotency_key=idempotency_key),
            json=payload,
            timeout=15.0,
        )

    def send_location(
        self,
        number: str,
        latitude: float,
        longitude: float,
        name: str = "",
        address: str = "",
        idempotency_key: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Call POST /send/location (GPS Location pin)."""
        payload: dict[str, Any] = {
            "number": _clean_recipient(number),
            "latitude": float(latitude),
            "longitude": float(longitude),
        }
        if name:
            payload["name"] = name
        if address:
            payload["address"] = address
        if idempotency_key:
            payload["idempotency_key"] = idempotency_key
            payload["idempotencyKey"] = idempotency_key
        return self._request(
            "POST",
            f"{self.api_url}/send/location",
            headers=self._headers(idempotency_key=idempotency_key),
            json=payload,
            timeout=15.0,
        )

    def send_contact(
        self,
        number: str,
        full_name: str,
        phone: str,
        organization: str = "",
        idempotency_key: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Call POST /send/contact (vCard contact card)."""
        payload: dict[str, Any] = {
            "number": _clean_recipient(number),
            "vcard": {
                "fullName": full_name,
                "phone": _clean_phone(phone),
                "organization": organization or None,
            },
        }
        if idempotency_key:
            payload["idempotency_key"] = idempotency_key
            payload["idempotencyKey"] = idempotency_key
        return self._request(
            "POST",
            f"{self.api_url}/send/contact",
            headers=self._headers(idempotency_key=idempotency_key),
            json=payload,
            timeout=15.0,
        )

    def send_buttons(
        self,
        number: str,
        title: str,
        description: str,
        footer: str,
        buttons: list[dict[str, Any]],
        idempotency_key: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Call POST /send/button."""
        payload: dict[str, Any] = {
            "number": _clean_recipient(number),
            "title": title,
            "description": description,
            "footer": footer,
            "buttons": buttons,
        }
        if idempotency_key:
            payload["idempotency_key"] = idempotency_key
            payload["idempotencyKey"] = idempotency_key
        return self._request(
            "POST",
            f"{self.api_url}/send/button",
            headers=self._headers(idempotency_key=idempotency_key),
            json=payload,
            timeout=15.0,
        )

    def send_list(
        self,
        number: str,
        title: str,
        description: str,
        footer_text: str,
        button_text: str,
        sections: list[dict[str, Any]],
        idempotency_key: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Call POST /send/list."""
        payload: dict[str, Any] = {
            "number": _clean_recipient(number),
            "title": title,
            "description": description,
            "footerText": footer_text,
            "buttonText": button_text or "Ver Opções",
            "sections": sections,
        }
        if idempotency_key:
            payload["idempotency_key"] = idempotency_key
            payload["idempotencyKey"] = idempotency_key
        return self._request(
            "POST",
            f"{self.api_url}/send/list",
            headers=self._headers(idempotency_key=idempotency_key),
            json=payload,
            timeout=15.0,
        )

    # -----------------------------------------------------------------------
    # API de Gerenciamento de Grupos da Evolution GO (Item 2)
    # -----------------------------------------------------------------------

    def list_groups(self) -> list[dict[str, Any]]:
        """Call GET /group/list or /group/fetchAllGroups and normalize returned groups."""
        for endpoint in ("/group/list", "/group/fetchAllGroups"):
            try:
                res = self._request(
                    "GET",
                    f"{self.api_url}{endpoint}",
                    headers=self._headers(),
                    timeout=20.0,
                )
                raw_list = []
                if isinstance(res, list):
                    raw_list = res
                elif isinstance(res, dict):
                    raw_list = res.get("data") or res.get("groups") or res.get("result") or []

                if isinstance(raw_list, list):
                    groups: list[dict[str, Any]] = []
                    for item in raw_list:
                        if not isinstance(item, dict):
                            continue
                        jid = str(item.get("JID") or item.get("Jid") or item.get("jid") or item.get("id") or "")
                        if not jid or "@g.us" not in jid:
                            continue
                        name = str(item.get("Name") or item.get("name") or item.get("Subject") or item.get("subject") or jid)
                        topic = str(item.get("Topic") or item.get("Description") or item.get("description") or "")
                        owner = str(item.get("OwnerJID") or item.get("owner") or "")
                        announce = bool(item.get("Announce") or item.get("announce") or False)
                        participants = item.get("Participants") or item.get("participants") or []

                        groups.append({
                            "jid": jid,
                            "name": name,
                            "description": topic,
                            "owner": owner,
                            "is_announce": announce,
                            "participants_count": len(participants) if isinstance(participants, list) else 0,
                            "participants": participants if isinstance(participants, list) else [],
                        })
                    return groups
            except Exception as exc:
                logger.info("EvolutionClient: endpoint %s returned error: %s", endpoint, exc)
                continue
        return []

    def get_group_info(self, group_jid: str) -> dict[str, Any]:
        """Fetch details of one specific group."""
        try:
            res = self._request(
                "GET",
                f"{self.api_url}/group/info",
                headers=self._headers(),
                params={"groupJid": group_jid},
                timeout=15.0,
            )
            return res if isinstance(res, dict) else {}
        except Exception as exc:
            logger.warning("EvolutionClient: could not fetch group info for %s: %s", group_jid, exc)
            return {}

    def create_group(self, group_name: str, participants: list[str]) -> dict[str, Any]:
        """Call POST /group/create to form a new WhatsApp group."""
        payload = {
            "groupName": group_name,
            "participants": [_clean_phone(p) for p in participants if p],
        }
        return self._request(
            "POST",
            f"{self.api_url}/group/create",
            headers=self._headers(),
            json=payload,
            timeout=20.0,
        )

    def update_group_subject(self, group_jid: str, subject: str) -> dict[str, Any]:
        """Call POST /group/updateSubject to rename a group."""
        payload = {
            "groupJid": group_jid,
            "groupName": subject[:100],
        }
        return self._request(
            "POST",
            f"{self.api_url}/group/updateSubject",
            headers=self._headers(),
            json=payload,
            timeout=15.0,
        )

    def update_group_description(self, group_jid: str, description: str) -> dict[str, Any]:
        """Call POST /group/updateDescription to update description."""
        payload = {
            "groupJid": group_jid,
            "description": description,
        }
        return self._request(
            "POST",
            f"{self.api_url}/group/updateDescription",
            headers=self._headers(),
            json=payload,
            timeout=15.0,
        )

    def update_group_settings(self, group_jid: str, action: str) -> dict[str, Any]:
        """Call POST /group/updateSetting (action: 'announcement' or 'not_announcement')."""
        payload = {
            "groupJid": group_jid,
            "action": action,
        }
        return self._request(
            "POST",
            f"{self.api_url}/group/updateSetting",
            headers=self._headers(),
            json=payload,
            timeout=15.0,
        )

    def leave_group(self, group_jid: str) -> dict[str, Any]:
        """Call POST /group/leaveGroup."""
        payload = {
            "groupJid": group_jid,
        }
        return self._request(
            "POST",
            f"{self.api_url}/group/leaveGroup",
            headers=self._headers(),
            json=payload,
            timeout=15.0,
        )

    def get_group_invite_code(self, group_jid: str) -> str:
        """Fetch invite code / link for the group."""
        try:
            res = self._request(
                "GET",
                f"{self.api_url}/group/inviteCode",
                headers=self._headers(),
                params={"groupJid": group_jid},
                timeout=15.0,
            )
            code = ""
            if isinstance(res, dict):
                code = str(res.get("code") or res.get("inviteCode") or res.get("link") or "")
            return code
        except Exception as exc:
            logger.warning("EvolutionClient: could not get invite code for %s: %s", group_jid, exc)
            return ""

    def revoke_group_invite_code(self, group_jid: str) -> str:
        """Revoke current invite link and generate a new one."""
        try:
            res = self._request(
                "POST",
                f"{self.api_url}/group/revokeInviteCode",
                headers=self._headers(),
                json={"groupJid": group_jid},
                timeout=15.0,
            )
            code = ""
            if isinstance(res, dict):
                code = str(res.get("code") or res.get("inviteCode") or res.get("link") or "")
            return code
        except Exception as exc:
            logger.warning("EvolutionClient: could not revoke invite code for %s: %s", group_jid, exc)
            return ""


def _normalize_evolution_button(btn: dict[str, Any]) -> dict[str, Any] | None:
    b_type = str(btn.get("type") or btn.get("action") or "reply").lower().strip()
    label = str(btn.get("displayText") or btn.get("label") or btn.get("text") or "").strip()
    if b_type == "pix":
        key = str(btn.get("key") or btn.get("pix_key") or "").strip()
        key_type = str(btn.get("keyType") or btn.get("pix_key_type") or "phone").strip().lower()
        if not key:
            return None
        res: dict[str, Any] = {
            "type": "pix",
            "key": key,
            "keyType": key_type,
            "currency": str(btn.get("currency") or "BRL"),
        }
        name = str(btn.get("name") or btn.get("pix_name") or "").strip()
        if name:
            res["name"] = name
        return res
    if not label:
        return None
    if b_type == "copy":
        copy_code = str(btn.get("copyCode") or btn.get("copy_code") or "").strip()
        if not copy_code:
            return None
        return {"type": "copy", "displayText": label, "copyCode": copy_code}
    if b_type == "url":
        url = str(btn.get("url") or "").strip()
        if not url:
            return None
        return {"type": "url", "displayText": label, "url": url}
    if b_type == "call":
        phone = re.sub(r"\D+", "", str(btn.get("phoneNumber") or btn.get("phone_number") or btn.get("phone") or ""))
        if not phone:
            return None
        return {"type": "call", "displayText": label, "phoneNumber": phone}
    b_id = str(btn.get("id") or btn.get("button_id") or "").strip() or f"btn-{hashlib.sha1(label.encode()).hexdigest()[:8]}"
    return {"type": "reply", "displayText": label, "id": b_id}


def _normalize_list_sections(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = []
    for sec in sections:
        if not isinstance(sec, dict):
            continue
        title = str(sec.get("title") or "").strip()
        rows = []
        for r in sec.get("rows") or []:
            if not isinstance(r, dict):
                continue
            r_title = str(r.get("title") or "").strip()
            if not r_title:
                continue
            row_dict: dict[str, Any] = {"title": r_title}
            desc = str(r.get("description") or "").strip()
            if desc:
                row_dict["description"] = desc
            row_id = str(r.get("rowId") or r.get("row_id") or r.get("id") or "").strip()
            if row_id:
                row_dict["rowId"] = row_id
            rows.append(row_dict)
        if rows:
            sec_dict: dict[str, Any] = {"rows": rows}
            if title:
                sec_dict["title"] = title
            normalized.append(sec_dict)
    return normalized


class WhatsAppEvolutionAdapter(Adapter):
    """Adapter integrating WhatsApp via Evolution GO."""

    platform = Platform.WHATSAPP_EVOLUTION.value
    capabilities = _CAPABILITIES
    webhook_content = "json"

    def get_client(self, connection: ChannelConnection, client: httpx.Client | None = None) -> EvolutionClient:
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
        return EvolutionClient(api_url=api_url, api_key=api_key, instance_id=instance_id, client=client)

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
        """Validate that the webhook callback is authentic.

        Fails closed: fails if the connection is disabled, or if neither the configured
        webhook secret nor the instance API key match using constant-time comparison.
        """
        if not connection or connection.platform != self.platform:
            return False

        if connection.status == ConnectionStatus.DISABLED:
            return False

        # 1. If webhook_secret is set on the connection, verify it
        secret = request.headers.get("X-Webhook-Secret") or request.GET.get("secret")
        if connection.webhook_secret_digest:
            return bool(secret and connection.verify_webhook_secret(secret))

        # 2. Accept matching apikey / x-api-key header using constant-time compare
        req_key = request.headers.get("apikey") or request.headers.get("x-api-key")
        expected_key = (
            connection.credentials.get("instance_token")
            or connection.credentials.get("api_key")
            or ""
        )
        if req_key and expected_key:
            return secrets.compare_digest(str(req_key), str(expected_key))

        return False

    def parse_events(self, request: Any, connection: ChannelConnection) -> list[NormalizedEvent]:
        """Convert Evolution GO webhook events into NormalizedEvent objects."""
        import json

        try:
            payload = json.loads(request.body)
        except Exception:
            return []

        raw_event = str(
            payload.get("event")
            or payload.get("type")
            or payload.get("eventType")
            or (payload.get("data") if isinstance(payload.get("data"), dict) else {}).get("event")
            or ""
        ).strip().lower()
        event_name = raw_event.replace("_", ".").replace("-", ".")
        data = payload.get("data", {})
        if not event_name and ("key" in payload or (isinstance(data, dict) and "key" in data)):
            event_name = "messages.upsert" 
        data = payload.get("data", {})

        # 1. Update Connection Status
        if event_name in {"connection.update", "status.instance", "instance.status", "connection"}:
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
        if event_name in {"qrcode.updated", "qrcode", "qr.updated"}:
            qr = data.get("qrcode") or data.get("base64") or ""
            session = EvolutionSession.objects.filter(connection=connection).first()
            if session and qr:
                session.qr_code_base64 = qr
                session.state = "qrcode"
                session.save(update_fields=["qr_code_base64", "state", "updated_at"])
            return []

        # 3. Message Receipt / Status Update
        if event_name in {"messages.update", "message.receipt", "read.receipt", "read_receipt", "message.update", "receipt"}:
            key = data.get("key", {})
            msg_id = key.get("id") or data.get("id") or ""
            status_val = (data.get("status") or data.get("receipt") or "").lower()
            if msg_id and status_val:
                mapped_status = "read" if "read" in status_val or "play" in status_val else "delivered"
                receipt_phone = _clean_phone(key.get("remoteJid", ""))
                return [
                    NormalizedEvent(
                        type=EventType.DELIVERY_STATUS,
                        connection=connection,
                        platform_user_id=f"+{receipt_phone}" if receipt_phone else key.get("remoteJid", ""),
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
        if event_name in {"message", "messages.upsert", "messages.create", "message.upsert", "messages.set", "send.message"}:
            items = []
            raw_data = payload.get("data")
            if isinstance(raw_data, dict):
                if isinstance(raw_data.get("messages"), list):
                    items = raw_data["messages"]
                else:
                    items = [raw_data]
            elif isinstance(raw_data, list):
                items = raw_data
            elif isinstance(payload.get("messages"), list):
                items = payload["messages"]
            elif isinstance(payload.get("key"), dict):
                items = [payload]
            else:
                items = [data] if data else []

            events: list[NormalizedEvent] = []
            for item in items:
                if not isinstance(item, dict):
                    continue

                info = item.get("Info") if isinstance(item.get("Info"), dict) else {}
                key = item.get("key") if isinstance(item.get("key"), dict) else {}

                from_me = bool(
                    item.get("fromMe")
                    or key.get("fromMe")
                    or info.get("IsFromMe")
                )
                if from_me:
                    continue

                remote_jid = str(
                    info.get("Chat")
                    or key.get("remoteJid")
                    or item.get("remoteJid")
                    or item.get("from")
                    or ""
                )
                if not remote_jid or "@g.us" in remote_jid or "@broadcast" in remote_jid or info.get("IsGroup"):
                    continue

                msg_id = str(key.get("id") or item.get("id") or info.get("ID") or info.get("id") or "")
                if not msg_id:
                    continue

                phone = _clean_phone(remote_jid)
                push_name = str(item.get("pushName") or item.get("senderName") or "")
                msg_content = item.get("Message") or item.get("message") or {}
                if not isinstance(msg_content, dict):
                    msg_content = {}
                if not msg_content and "conversation" in item:
                    msg_content = item

                button_id = ""
                text = ""

                # 4.1 Buttons response
                btn_resp = msg_content.get("buttonsResponseMessage")
                if isinstance(btn_resp, dict):
                    button_id = str(btn_resp.get("selectedButtonId") or "")
                    text = str(btn_resp.get("selectedDisplayText") or button_id)

                # 4.2 Template button reply
                if not button_id:
                    tpl_resp = msg_content.get("templateButtonReplyMessage")
                    if isinstance(tpl_resp, dict):
                        button_id = str(tpl_resp.get("selectedId") or "")
                        text = str(tpl_resp.get("selectedDisplayText") or button_id)

                # 4.3 List response
                if not button_id:
                    list_resp = msg_content.get("listResponseMessage")
                    if isinstance(list_resp, dict):
                        single_select = list_resp.get("singleSelectReply") if isinstance(list_resp.get("singleSelectReply"), dict) else {}
                        button_id = str(single_select.get("selectedRowId") or list_resp.get("rowId") or "")
                        text = str(list_resp.get("title") or list_resp.get("description") or button_id)

                # 4.4 Interactive response (Native Flow)
                if not button_id:
                    interactive_resp = msg_content.get("interactiveResponseMessage")
                    if isinstance(interactive_resp, dict):
                        body_txt = interactive_resp.get("body", {}).get("text") if isinstance(interactive_resp.get("body"), dict) else None
                        if body_txt:
                            text = str(body_txt)
                        native_flow = interactive_resp.get("nativeFlowResponseMessage")
                        if isinstance(native_flow, dict):
                            params_json = native_flow.get("paramsJson")
                            if isinstance(params_json, str):
                                try:
                                    decoded = json.loads(params_json)
                                    if isinstance(decoded, dict):
                                        button_id = str(decoded.get("id") or "")
                                        cand_title = str(decoded.get("title") or "")
                                        if cand_title:
                                            text = cand_title
                                except Exception:
                                    pass

                # 4.5 Text / Extended text / Captions
                if not text:
                    text = str(
                        msg_content.get("conversation")
                        or (msg_content.get("extendedTextMessage", {}).get("text") if isinstance(msg_content.get("extendedTextMessage"), dict) else "")
                        or ""
                    )

                attachments: list[str] = []
                caption = ""
                for m_key in ("imageMessage", "videoMessage", "audioMessage", "documentMessage"):
                    if m_key in msg_content and isinstance(msg_content[m_key], dict):
                        media_info = msg_content[m_key]
                        media_url = str(media_info.get("url") or "")
                        caption = str(media_info.get("caption") or "")
                        if media_url:
                            attachments.append(media_url)
                        break

                if not text and caption:
                    text = caption

                ts = timezone.now()
                raw_ts = item.get("messageTimestamp") or (data.get("messageTimestamp") if isinstance(data, dict) else None)
                if raw_ts:
                    try:
                        ts = datetime.fromtimestamp(int(raw_ts), tz=dt_timezone.utc)
                    except Exception:
                        pass

                if button_id:
                    events.append(
                        NormalizedEvent(
                            type=EventType.POSTBACK,
                            connection=connection,
                            platform_user_id=f"+{phone}" if phone else remote_jid,
                            provider_event_id=msg_id,
                            timestamp=ts,
                            payload=EventPayload(
                                button_id=button_id,
                                text=text,
                                attachments=tuple(attachments),
                                extra={
                                    "reply_id": button_id,
                                    "sender_name": push_name,
                                    "phone": phone,
                                    "remote_jid": remote_jid,
                                },
                            ),
                            raw=item,
                        )
                    )
                elif text or attachments:
                    events.append(
                        NormalizedEvent(
                            type=EventType.MESSAGE,
                            connection=connection,
                            platform_user_id=f"+{phone}" if phone else remote_jid,
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
                            raw=item,
                        )
                    )

            return events

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

        recipient_raw = str(outbound.recipient_override or getattr(identity, "platform_user_id", ""))
        number = _clean_recipient(recipient_raw)
        if not _is_supported_recipient(number):
            raise APIError(
                f"Invalid or unsupported WhatsApp recipient: {recipient_raw}",
                status_code=400,
                code="recipient_unsupported",
            )

        idempotency_key = (
            getattr(outbound, "idempotency_key", "")
            or (getattr(outbound, "extra", {}) or {}).get("idempotency_key", "")
            or ""
        )
        client = self.get_client(connection)
        rendered = downgrade(outbound, self.capabilities)

        last_provider_id = ""
        for message in rendered.messages:
            body = message.to_body()
            blocks = list(body.get("blocks", []))
            raw_buttons = list(body.get("buttons", []))
            raw_qrs = list(body.get("quick_replies", []))

            combined_buttons = list(raw_buttons)
            if raw_qrs:
                existing_ids = {
                    str(b.get("id") or b.get("button_id") or "").strip()
                    for b in raw_buttons
                    if isinstance(b, dict) and (b.get("id") or b.get("button_id"))
                }
                for qr in raw_qrs:
                    if isinstance(qr, dict) and qr.get("label"):
                        qr_id = str(qr.get("id") or qr.get("button_id") or "").strip()
                        if not qr_id or qr_id not in existing_ids:
                            combined_buttons.append({
                                "id": qr.get("id"),
                                "label": str(qr.get("label") or "").strip(),
                                "action": "postback",
                                "type": "reply",
                            })
                            if qr_id:
                                existing_ids.add(qr_id)

            norm_buttons = []
            for b in combined_buttons:
                norm = _normalize_evolution_button(b)
                if norm:
                    norm_buttons.append(norm)

            buttons_sent = False
            for block in blocks:
                b_type = block.get("type")
                if b_type == "text":
                    text_val = str(block.get("text") or "")
                    if not text_val:
                        continue
                    if norm_buttons and not buttons_sent:
                        try:
                            res = client.send_buttons(
                                number=number,
                                title="",
                                description=text_val,
                                footer="",
                                buttons=norm_buttons,
                            )
                            last_provider_id = str(res.get("key", {}).get("id") or res.get("id") or last_provider_id)
                            buttons_sent = True
                        except APIError as exc:
                            logger.error("Evolution GO send_buttons failed for connection %s: %s", connection.pk, exc)
                            return SendResult(SendStatus.FAILED, error=exc.code or "send_failed")
                    else:
                        try:
                            res = client.send_text(number=number, text=text_val)
                            last_provider_id = str(res.get("key", {}).get("id") or res.get("id") or last_provider_id)
                        except APIError as exc:
                            logger.error("Evolution GO send_text failed for connection %s: %s", connection.pk, exc)
                            return SendResult(SendStatus.FAILED, error=exc.code or "send_failed")

                elif b_type in ("image", "video", "audio", "file", "ptt"):
                    media_url = str(block.get("url", ""))
                    if not media_url:
                        continue
                    m_kind = str(block.get("type", "image"))
                    caption = str(block.get("caption") or block.get("text") or "")
                    filename = str(block.get("filename") or "")
                    is_ptt = bool(
                        block.get("is_ptt")
                        or m_kind == "ptt"
                        or "ptt" in filename.lower()
                        or "ptt" in media_url.lower()
                        or "ptt" in caption.lower()
                        or "voice" in caption.lower()
                        or block.get("extra", {}).get("ptt")
                    )
                    try:
                        res = client.send_media(
                            number=number,
                            media_type=m_kind,
                            url=media_url,
                            caption=caption,
                            filename=filename,
                            ptt=is_ptt,
                        )
                        last_provider_id = str(res.get("key", {}).get("id") or res.get("id") or last_provider_id)
                    except APIError as exc:
                        logger.error("Evolution GO send_media failed for connection %s: %s", connection.pk, exc)
                        return SendResult(SendStatus.FAILED, error=exc.code or "send_failed")

                elif b_type == "location":
                    try:
                        res = client.send_location(
                            number=number,
                            latitude=float(block.get("latitude", 0.0)),
                            longitude=float(block.get("longitude", 0.0)),
                            name=str(block.get("name", "")),
                            address=str(block.get("address", "")),
                        )
                        last_provider_id = str(res.get("key", {}).get("id") or res.get("id") or last_provider_id)
                    except APIError as exc:
                        logger.error("Evolution GO send_location failed: %s", exc)
                        return SendResult(SendStatus.FAILED, error=exc.code or "send_failed")

                elif b_type == "contact":
                    try:
                        res = client.send_contact(
                            number=number,
                            full_name=str(block.get("full_name", "")),
                            phone=str(block.get("phone", "")),
                            organization=str(block.get("organization", "")),
                        )
                        last_provider_id = str(res.get("key", {}).get("id") or res.get("id") or last_provider_id)
                    except APIError as exc:
                        logger.error("Evolution GO send_contact failed: %s", exc)
                        return SendResult(SendStatus.FAILED, error=exc.code or "send_failed")

                elif b_type == "poll":
                    try:
                        res = client.send_poll(
                            number=number,
                            question=str(block.get("question", "")),
                            options=list(block.get("options", [])),
                            max_answers=int(block.get("max_answers", 1)),
                        )
                        last_provider_id = str(res.get("key", {}).get("id") or res.get("id") or last_provider_id)
                    except APIError as exc:
                        logger.error("Evolution GO send_poll failed: %s", exc)
                        return SendResult(SendStatus.FAILED, error=exc.code or "send_failed")

                elif b_type == "list":
                    try:
                        sections = _normalize_list_sections(list(block.get("sections", [])))
                        res = client.send_list(
                            number=number,
                            title=str(block.get("title", "")),
                            description=str(block.get("description", "")),
                            footer_text=str(block.get("footer_text", "")),
                            button_text=str(block.get("button_text", "Ver Opções")),
                            sections=sections,
                        )
                        last_provider_id = str(res.get("key", {}).get("id") or res.get("id") or last_provider_id)
                    except APIError as exc:
                        logger.error("Evolution GO send_list failed: %s", exc)
                        return SendResult(SendStatus.FAILED, error=exc.code or "send_failed")

                elif b_type == "sticker":
                    url = str(block.get("url", ""))
                    if url:
                        try:
                            res = client.send_sticker(number=number, url=url)
                            last_provider_id = str(res.get("key", {}).get("id") or res.get("id") or last_provider_id)
                        except APIError as exc:
                            logger.error("Evolution GO send_sticker failed: %s", exc)
                            return SendResult(SendStatus.FAILED, error=exc.code or "send_failed")

            # If buttons exist but no text block was available, send buttons with default prompt
            if norm_buttons and not buttons_sent:
                try:
                    res = client.send_buttons(
                        number=number,
                        title="",
                        description="Opções:",
                        footer="",
                        buttons=norm_buttons,
                    )
                    last_provider_id = str(res.get("key", {}).get("id") or res.get("id") or last_provider_id)
                    buttons_sent = True
                except APIError as exc:
                    logger.error("Evolution GO send_buttons failed for connection %s: %s", connection.pk, exc)
                    return SendResult(SendStatus.FAILED, error=exc.code or "send_failed")

        if not last_provider_id:
            return SendResult(SendStatus.FAILED, error="no_content_sent")

        return SendResult(SendStatus.SENT, provider_message_id=last_provider_id[:200])

    def find_sent_message(self, connection: ChannelConnection, idempotency_key: str) -> str | None:
        """Query if a message with this idempotency key was already accepted/sent."""
        client = self.get_client(connection)
        if hasattr(client, "find_sent_message"):
            try:
                return client.find_sent_message(idempotency_key)
            except Exception:
                pass
        return None

    def on_disconnect(self, connection: ChannelConnection) -> None:
        """Called when connection is disconnected or disabled."""
        try:
            client = self.get_client(connection)
            client.disconnect()
        except Exception:
            logger.warning("Could not disconnect Evolution GO instance for connection %s", connection.pk)


from apps.channels.registry import register_adapter  # noqa: E402

register_adapter(Platform.WHATSAPP_EVOLUTION, WhatsAppEvolutionAdapter)
