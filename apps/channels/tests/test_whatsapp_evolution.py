"""Tests for Evolution GO WhatsApp transport and its SSRF protection.

Proves that calls made through EvolutionClient go through guarded_request (SECURITY-BASELINE §6)
and that requests to loopback, link-local / metadata addresses are blocked.
"""

from datetime import datetime, timezone as dt_timezone
import json
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from apps.channels.events import EventType
from apps.channels.models import ChannelConnection
from apps.channels.providers.whatsapp_evolution import (
    EvolutionClient,
    WhatsAppEvolutionAdapter,
)
from apps.common.outbound import BlockedURLError
from tests.ssrf import FakeInternet, deployment_cache_cleared, guard_required, serving

EVO_HOST = "evolution.example.test"
EVO_URL = f"https://{EVO_HOST}"
EVO_KEY = "evo_secret_key"
EVO_INSTANCE = "test_instance"

STATUS_PAYLOAD = b'{"state": "open", "instance": {"status": "open", "instanceName": "test"}}'


class TestEvolutionSSRFProtection:
    """Prove Evolution GO client HTTP requests go through guarded_request."""

    def test_client_requests_are_guarded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Calling EvolutionClient methods passes through the guard."""
        with deployment_cache_cleared():
            FakeInternet(serving(STATUS_PAYLOAD), {EVO_HOST: [FakeInternet.PUBLIC]}).install(monkeypatch)

            client = EvolutionClient(api_url=EVO_URL, api_key=EVO_KEY, instance_id=EVO_INSTANCE)

            with guard_required() as requests:
                res = client.get_status()

            assert res.get("state") == "open"
            assert len(requests) >= 1
            assert requests[0].headers.get("apikey") == EVO_KEY

    def test_loopback_address_is_blocked(self) -> None:
        """SSRF: Requests targeting 127.0.0.1 must be rejected by the guard."""
        with deployment_cache_cleared():
            client = EvolutionClient(api_url="http://127.0.0.1:8080", api_key=EVO_KEY, instance_id=EVO_INSTANCE)
            with pytest.raises(BlockedURLError):
                client.get_status()

    def test_metadata_address_is_blocked(self) -> None:
        """SSRF: Requests targeting 169.254.169.254 must be rejected by the guard."""
        with deployment_cache_cleared():
            client = EvolutionClient(api_url="http://169.254.169.254", api_key=EVO_KEY, instance_id=EVO_INSTANCE)
            with pytest.raises(BlockedURLError):
                client.get_status()


class TestEvolutionTimestampNormalization:
    """Verify Evolution GO message timestamp parsing with Django 5.2."""

    def test_message_timestamp_parses_with_dt_timezone_utc(self) -> None:
        """Epoch timestamp must convert to UTC datetime using dt_timezone.utc."""
        adapter = WhatsAppEvolutionAdapter()
        conn = MagicMock(spec=ChannelConnection)
        conn.platform = "whatsapp_evolution"
        conn.external_id = "test_instance"

        epoch = 1700000000
        expected_dt = datetime.fromtimestamp(epoch, tz=dt_timezone.utc)

        raw_payload = {
            "event": "messages.upsert",
            "instance": "test_instance",
            "data": {
                "key": {
                    "id": "MSG_12345",
                    "remoteJid": "5511999999999@s.whatsapp.net",
                    "fromMe": False,
                },
                "message": {
                    "conversation": "Hello there",
                },
                "messageTimestamp": epoch,
            },
        }

        mock_request = MagicMock()
        mock_request.body = json.dumps(raw_payload).encode("utf-8")

        events = adapter.parse_events(mock_request, conn)
        assert len(events) == 1
        event = events[0]
        assert event.type == EventType.MESSAGE
        assert event.timestamp == expected_dt
