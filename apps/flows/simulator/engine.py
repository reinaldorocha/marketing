"""Conversation Simulator Engine for Flow Builder.

Runs flow graphs in an isolated session stored in Django cache.
Executes real node classes and schema contracts without mutating production database tables
or sending real outbound traffic over channels.
"""

from __future__ import annotations

import copy
import logging
import unittest.mock
from dataclasses import asdict, dataclass, field
from datetime import date as d_date, datetime, timedelta, timezone as dt_timezone
from typing import Any
from uuid import UUID, uuid4

from django.core.cache import cache
from django.utils import timezone

from apps.channels.events import (
    Button,
    CardBlock,
    ContactBlock,
    GalleryBlock,
    ListBlock,
    LocationBlock,
    MediaBlock,
    OutboundMessage,
    PollBlock,
    QuickReply,
    StickerBlock,
    TextBlock,
)
from apps.common.platforms import Platform
from apps.flows.engine.context import NodeContext
from apps.flows.engine.graph import Graph
from apps.flows.engine.registry import node_class_for
from apps.flows.engine.results import Continue, End, Fail, Schedule, StepResult, Wait
from apps.flows.engine.sending import SendOutcome
from apps.flows.engine.waits import (
    WAIT_BUTTONS,
    WAIT_DATA_COLLECTION,
    InvalidAnswerError,
    normalise_answer,
)
from apps.flows.models import ExecutionStatus, Flow, FlowVersion, StartedBy
from apps.flows.rendering import context_for, render
from apps.workspaces.models import Workspace

logger = logging.getLogger(__name__)

CACHE_PREFIX = "flow_sim_"
CACHE_TTL_SECONDS = 3600  # 1 hour


class SimulatedChannelConnection:
    """Mock connection for simulation."""

    is_simulated = True

    def __init__(self, workspace: Workspace | None = None, connection_id: UUID | None = None) -> None:
        real_conn = None
        if workspace and connection_id is None:
            try:
                from apps.channels.models import ChannelConnection
                real_conn = ChannelConnection.objects.for_workspace(workspace).filter(platform=Platform.WHATSAPP_EVOLUTION.value).first()
            except Exception:
                pass
        self.pk = connection_id or (real_conn.pk if real_conn else uuid4())
        self.id = self.pk
        self.workspace = workspace
        self.workspace_id = workspace.pk if workspace else None
        self.platform = Platform.WHATSAPP_EVOLUTION
        self.name = "Simulador WhatsApp"


class SimulatedContact:
    """In-memory contact with attribute access and condition evaluation."""

    is_simulated = True

    def __init__(self, data: dict[str, Any], workspace: Workspace | None = None) -> None:
        self.pk = uuid4()
        self.id = self.pk
        self.workspace = workspace
        self.workspace_id = workspace.pk if workspace else None
        self.first_name = str(data.get("first_name") or "João")
        self.last_name = str(data.get("last_name") or "Silva")
        self.name = f"{self.first_name} {self.last_name}".strip()
        self.phone = str(data.get("phone") or "5511999999999")
        self.email = str(data.get("email") or "joao.silva@example.com")
        self.username = str(data.get("username") or data.get("instagram_username") or "")
        self.instagram_username = self.username
        fc = data.get("follower_count") if data.get("follower_count") is not None else data.get("instagram_follower_count")
        try:
            self.follower_count = int(fc) if fc is not None else 0
        except (ValueError, TypeError):
            self.follower_count = 0
        self.instagram_follower_count = self.follower_count
        foll = data.get("is_follower")
        if foll is None:
            foll = data.get("is_user_follow_business")
        self.is_follower = foll if foll is not None else True
        self.is_user_follow_business = self.is_follower
        self.is_business_follow_user = bool(data.get("is_business_follow_user", False))
        self.profile_pic = str(data.get("profile_pic") or "")
        self.follower_status = data.get("follower_status", "follows")
        self.custom_fields = dict(data.get("custom_fields") or {})
        raw_tags = data.get("tags") or []
        self.tags = set(raw_tags if isinstance(raw_tags, (list, set, tuple)) else [])

    def _evaluate_condition(self, filter_json: Any, *, now: datetime | None = None) -> bool:
        """Evaluate a filter document against this simulated contact in memory."""
        if not isinstance(filter_json, dict):
            return True
        match_mode = str(filter_json.get("match") or "all").lower()
        rules = filter_json.get("rules") or []
        if not rules:
            return match_mode == "all"

        ref_now = now or timezone.now()

        results: list[bool] = []
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            source = str(rule.get("source") or "")
            key = str(rule.get("key") or "")
            op = str(rule.get("op") or "is")
            expected = rule.get("value")

            actual = None
            if source in ("system", "system_field", "channel", "identity", "instagram"):
                if key in ("is_follower", "is_user_follow_business"):
                    actual = self.is_user_follow_business
                elif key in ("follower_count", "instagram_follower_count"):
                    actual = self.follower_count
                elif key in ("username", "instagram_username"):
                    actual = self.username
                elif key == "is_business_follow_user":
                    actual = self.is_business_follow_user
                else:
                    actual = getattr(self, key, "")
            elif source == "custom_field":
                # Resolve key: key may be a UUID or a field name
                cf_name = None
                try:
                    UUID(key)
                    if self.workspace:
                        from apps.contacts.models import CustomField
                        cf = CustomField.objects.for_workspace(self.workspace).filter(pk=key).first()
                        if cf:
                            cf_name = cf.name
                except (ValueError, TypeError):
                    pass

                if cf_name and cf_name in self.custom_fields:
                    actual = self.custom_fields[cf_name]
                elif key in self.custom_fields:
                    actual = self.custom_fields[key]
                else:
                    # Case-insensitive lookup in custom_fields
                    for k, v in self.custom_fields.items():
                        if (cf_name and k.casefold() == cf_name.casefold()) or k.casefold() == key.casefold():
                            actual = v
                            break

            elif source == "tag":
                tag_ref = str(rule.get("key") or expected or "").strip()
                tag_name = tag_ref
                tag_uuid_str = ""
                try:
                    UUID(tag_ref)
                    tag_uuid_str = tag_ref
                    if self.workspace:
                        from apps.contacts.models import Tag
                        t_obj = Tag.objects.for_workspace(self.workspace).filter(pk=tag_ref).first()
                        if t_obj:
                            tag_name = t_obj.name
                except (ValueError, TypeError):
                    pass

                has_it = any(
                    t.casefold() == tag_name.casefold()
                    or (tag_uuid_str and str(t).casefold() == tag_uuid_str.casefold())
                    for t in self.tags
                )
                if op in ("has", "has_tag", "has_any", "is", "equals"):
                    results.append(has_it)
                elif op in ("has_not", "not_equals", "is_not"):
                    results.append(not has_it)
                else:
                    results.append(False)
                continue

            elif source == "segment":
                # Resolve segment by UUID
                in_segment = False
                try:
                    UUID(key)
                    if self.workspace:
                        from apps.contacts.models import Segment
                        seg = Segment.objects.for_workspace(self.workspace).filter(pk=key).first()
                        if seg and seg.filter_json:
                            in_segment = self._evaluate_condition(seg.filter_json, now=ref_now)
                except (ValueError, TypeError):
                    pass

                if op in ("in", "has", "is"):
                    results.append(in_segment)
                elif op in ("not_in", "has_not", "is_not"):
                    results.append(not in_segment)
                else:
                    results.append(False)
                continue
            else:
                results.append(False)
                continue

            # Date operations (resolved in the workspace's effective timezone)
            is_date_op = op in ("before", "after", "on") or (
                op in ("is", "equals", "==")
                and (
                    isinstance(expected, (datetime, d_date))
                    or getattr(expected, "__class__", None).__name__ == "RelativeDay"
                    or (isinstance(expected, dict) and ("relative" in expected or "offset" in expected))
                )
            )
            if is_date_op:
                from apps.contacts.conditions import RelativeDay, _workspace_timezone

                try:
                    ws_tz = _workspace_timezone(self.workspace)
                except Exception:
                    from zoneinfo import ZoneInfo
                    tz_name = getattr(self.workspace, "effective_timezone", None) if self.workspace else None
                    try:
                        ws_tz = ZoneInfo(tz_name or "UTC")
                    except Exception:
                        ws_tz = dt_timezone.utc

                if timezone.is_naive(ref_now):
                    ref_now = timezone.make_aware(ref_now, dt_timezone.utc)
                local_ref_now = timezone.localtime(ref_now, ws_tz)

                actual_date = None
                if isinstance(actual, datetime):
                    dt_act = actual
                    if timezone.is_naive(dt_act):
                        dt_act = timezone.make_aware(dt_act, dt_timezone.utc)
                    actual_date = timezone.localtime(dt_act, ws_tz).date()
                elif isinstance(actual, d_date):
                    actual_date = actual
                elif isinstance(actual, str) and actual.strip():
                    try:
                        s_val = actual.strip()
                        if "T" in s_val:
                            dt_act = datetime.fromisoformat(s_val.replace("Z", "+00:00"))
                            if timezone.is_naive(dt_act):
                                dt_act = timezone.make_aware(dt_act, dt_timezone.utc)
                            actual_date = timezone.localtime(dt_act, ws_tz).date()
                        else:
                            actual_date = d_date.fromisoformat(s_val)
                    except (ValueError, TypeError):
                        pass

                expected_date = None
                if isinstance(expected, RelativeDay):
                    expected_date = expected.resolve(ref_now, ws_tz)
                elif isinstance(expected, dict):
                    if "relative" in expected and isinstance(expected["relative"], dict):
                        offset = int(expected["relative"].get("offset") or 0)
                        expected_date = local_ref_now.date() + timedelta(days=offset)
                    elif "offset" in expected:
                        offset = int(expected.get("offset") or 0)
                        expected_date = local_ref_now.date() + timedelta(days=offset)
                elif isinstance(expected, datetime):
                    dt_exp = expected
                    if timezone.is_naive(dt_exp):
                        dt_exp = timezone.make_aware(dt_exp, dt_timezone.utc)
                    expected_date = timezone.localtime(dt_exp, ws_tz).date()
                elif isinstance(expected, d_date):
                    expected_date = expected
                elif isinstance(expected, str) and expected.strip():
                    try:
                        s_exp = expected.strip()
                        if "T" in s_exp:
                            dt_exp = datetime.fromisoformat(s_exp.replace("Z", "+00:00"))
                            if timezone.is_naive(dt_exp):
                                dt_exp = timezone.make_aware(dt_exp, dt_timezone.utc)
                            expected_date = timezone.localtime(dt_exp, ws_tz).date()
                        else:
                            expected_date = d_date.fromisoformat(s_exp)
                    except (ValueError, TypeError):
                        pass

                if actual_date is None or expected_date is None:
                    matched = False
                elif op == "before":
                    matched = actual_date < expected_date
                elif op == "after":
                    matched = actual_date > expected_date
                elif op in ("on", "is", "equals", "=="):
                    matched = actual_date == expected_date
                else:
                    matched = False
                results.append(matched)
                continue

            s_act = str(actual if actual is not None else "").strip()
            s_exp = str(expected if expected is not None else "").strip()

            if op in ("is", "equals"):
                if isinstance(expected, bool):
                    if isinstance(actual, bool):
                        matched = actual == expected
                    else:
                        act_b = s_act.lower() in ("true", "1", "yes", "sim")
                        matched = act_b == expected
                else:
                    matched = s_act.casefold() == s_exp.casefold()
            elif op in ("is_not", "not_equals"):
                if isinstance(expected, bool):
                    if isinstance(actual, bool):
                        matched = actual != expected
                    else:
                        act_b = s_act.lower() in ("true", "1", "yes", "sim")
                        matched = act_b != expected
                else:
                    matched = s_act.casefold() != s_exp.casefold()
            elif op == "contains":
                matched = bool(s_exp and s_exp.casefold() in s_act.casefold())
            elif op == "not_contains":
                matched = bool(not s_exp or s_exp.casefold() not in s_act.casefold())
            elif op == "starts_with":
                matched = bool(s_exp and s_act.casefold().startswith(s_exp.casefold()))
            elif op == "ends_with":
                matched = bool(s_exp and s_act.casefold().endswith(s_exp.casefold()))
            elif op == "has_value":
                matched = bool(s_act)
            elif op == "no_value":
                matched = not bool(s_act)
            elif op in ("=", "!=", ">", "<", ">=", "<="):
                try:
                    num_act = float(actual)
                    num_exp = float(expected)
                    if op == "=":
                        matched = num_act == num_exp
                    elif op == "!=":
                        matched = num_act != num_exp
                    elif op == ">":
                        matched = num_act > num_exp
                    elif op == "<":
                        matched = num_act < num_exp
                    elif op == ">=":
                        matched = num_act >= num_exp
                    elif op == "<=":
                        matched = num_act <= num_exp
                    else:
                        matched = False
                except (ValueError, TypeError):
                    matched = False
            else:
                matched = False
            results.append(matched)

        if match_mode == "any":
            return any(results)
        return all(results)

class SimulatedExecution:
    """Mock execution mimicking FlowExecution for engine nodes."""

    def __init__(
        self,
        session: FlowSimulatorSession,
        workspace: Workspace,
        flow: Flow | None,
        contact: SimulatedContact,
        connection: SimulatedChannelConnection,
    ) -> None:
        self.pk = uuid4()
        self.id = self.pk
        self.workspace = workspace
        self.workspace_id = workspace.pk if workspace else None
        self.flow = flow
        self.flow_version = None
        self.contact = contact
        self.contact_id = contact.pk
        self.channel_connection = connection
        self.channel_connection_id = connection.pk
        self.variables = session.variables
        self.status = ExecutionStatus.RUNNING
        self.current_node_id = session.current_node_id or ""
        self.blocks_since_pause = 0
        self.wait_config = session.wait_config
        self.started_by = StartedBy.MANUAL
        self.preview = True
        self.is_live = True
        self.is_simulated = True
        self.now = session.get_virtual_datetime()
        self._simulator_transport = SimulatedTransport(session)

    def save(self, *args: Any, **kwargs: Any) -> None:
        """In-memory execution; no DB persistence."""
        pass


class SimulatedTransport:
    """Intercepts deliver() calls and serializes messages into session trace."""

    def __init__(self, session: FlowSimulatorSession) -> None:
        self.session = session

    def deliver(
        self,
        execution: Any,
        outbound: OutboundMessage,
        *,
        node_id: str,
        attempt: int = 0,
    ) -> SendOutcome:
        serialized_blocks = []
        for block in outbound.blocks:
            if isinstance(block, TextBlock):
                serialized_blocks.append({"type": "text", "text": block.text})
            elif isinstance(block, MediaBlock):
                serialized_blocks.append({"type": block.kind, "url": block.url, "caption": block.caption})
            elif isinstance(block, CardBlock):
                serialized_blocks.append({
                    "type": "card",
                    "card": {
                        "title": block.card.title,
                        "subtitle": block.card.subtitle,
                        "image_url": block.card.image_url,
                        "url": block.card.url,
                        "buttons": [
                            {"id": btn.id, "label": btn.label, "url": btn.url, "action": btn.action}
                            for btn in block.card.buttons
                        ],
                    },
                })
            elif isinstance(block, GalleryBlock):
                serialized_blocks.append({
                    "type": "gallery",
                    "cards": [
                        {
                            "title": c.title,
                            "subtitle": c.subtitle,
                            "image_url": c.image_url,
                            "url": c.url,
                            "buttons": [{"id": b.id, "label": b.label, "url": b.url, "action": b.action} for b in c.buttons],
                        }
                        for c in block.cards
                    ],
                })
            elif isinstance(block, LocationBlock):
                serialized_blocks.append({
                    "type": "location",
                    "latitude": block.latitude,
                    "longitude": block.longitude,
                    "name": block.name,
                    "address": block.address,
                })
            elif isinstance(block, ContactBlock):
                serialized_blocks.append({
                    "type": "contact",
                    "full_name": block.full_name,
                    "phone": block.phone,
                    "organization": block.organization,
                })
            elif isinstance(block, PollBlock):
                serialized_blocks.append({
                    "type": "poll",
                    "question": block.question,
                    "options": block.options,
                    "max_answers": block.max_answers,
                })
            elif isinstance(block, ListBlock):
                serialized_blocks.append({
                    "type": "list",
                    "title": block.title,
                    "button_text": block.button_text,
                    "sections": block.sections,
                })
            elif isinstance(block, StickerBlock):
                serialized_blocks.append({"type": "sticker", "url": block.url})

        serialized_buttons = [
            {
                "id": b.id,
                "label": b.label,
                "action": b.action,
                "url": b.url,
                "copy_code": b.copy_code,
                "phone_number": b.phone_number,
                "pix_key": b.pix_key,
                "pix_key_type": b.pix_key_type,
                "pix_name": b.pix_name,
                "currency": b.currency,
            }
            for b in outbound.buttons
        ]

        serialized_qrs = [{"id": qr.id, "label": qr.label} for qr in outbound.quick_replies]

        self.session.trace.append({
            "type": "bot_message",
            "timestamp": self.session.virtual_time,
            "node_id": node_id,
            "blocks": serialized_blocks,
            "buttons": serialized_buttons,
            "quick_replies": serialized_qrs,
            "recipient_override": outbound.recipient_override or "",
        })
        return SendOutcome(sent=True, message=None)


class FlowSimulatorSession:
    """Manages an isolated conversation simulation session."""

    def __init__(
        self,
        session_id: str,
        workspace_id: str,
        flow_id: str,
        graph: dict[str, Any],
        virtual_contact: dict[str, Any] | None = None,
        variables: dict[str, Any] | None = None,
        virtual_time: str | None = None,
        status: str = "idle",
        current_node_id: str | None = None,
        wait_config: dict[str, Any] | None = None,
        visited_nodes: list[str] | None = None,
        trace: list[dict[str, Any]] | None = None,
        initial_contact: dict[str, Any] | None = None,
        initial_variables: dict[str, Any] | None = None,
        user_id: str = "",
    ) -> None:
        self.user_id = user_id
        self.session_id = session_id
        self.workspace_id = workspace_id
        self.flow_id = flow_id
        self.graph = graph
        base_contact = virtual_contact if virtual_contact is not None else {
            "first_name": "João",
            "last_name": "Silva",
            "phone": "5511999999999",
            "email": "joao.silva@example.com",
            "username": "joaosilva",
            "follower_count": 150,
            "is_follower": True,
            "follower_status": "follows",
            "custom_fields": {},
            "tags": [],
        }
        self.virtual_contact = copy.deepcopy(base_contact)
        self.variables = copy.deepcopy(variables or {})
        self.virtual_time = virtual_time or timezone.now().isoformat()
        self.status = status
        self.current_node_id = current_node_id
        self.wait_config = copy.deepcopy(wait_config or {})
        self.visited_nodes = list(visited_nodes or [])
        self.trace = list(trace or [])
        self.initial_contact = copy.deepcopy(initial_contact if initial_contact is not None else base_contact)
        self.initial_variables = copy.deepcopy(initial_variables if initial_variables is not None else self.variables)

    def get_virtual_datetime(self) -> datetime:
        """Get aware datetime representing current virtual time without mocking global timezone."""
        try:
            dt = datetime.fromisoformat(self.virtual_time)
            if timezone.is_naive(dt):
                dt = timezone.make_aware(dt, timezone.utc)
            return dt
        except Exception:
            return timezone.now()

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "workspace_id": self.workspace_id,
            "flow_id": self.flow_id,
            "user_id": self.user_id,
            "graph": self.graph,
            "virtual_contact": self.virtual_contact,
            "variables": self.variables,
            "virtual_time": self.virtual_time,
            "status": self.status,
            "current_node_id": self.current_node_id,
            "wait_config": self.wait_config,
            "visited_nodes": self.visited_nodes,
            "trace": self.trace,
            "initial_contact": self.initial_contact,
            "initial_variables": self.initial_variables,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FlowSimulatorSession:
        return cls(
            session_id=data["session_id"],
            workspace_id=data["workspace_id"],
            flow_id=data["flow_id"],
            user_id=data.get("user_id", ""),
            graph=data["graph"],
            virtual_contact=data.get("virtual_contact"),
            variables=data.get("variables"),
            virtual_time=data.get("virtual_time"),
            status=data.get("status", "idle"),
            current_node_id=data.get("current_node_id"),
            wait_config=data.get("wait_config"),
            visited_nodes=data.get("visited_nodes"),
            trace=data.get("trace"),
            initial_contact=data.get("initial_contact"),
            initial_variables=data.get("initial_variables"),
        )

    def save(self) -> None:
        key = f"{CACHE_PREFIX}{self.session_id}"
        cache.set(key, self.to_dict(), timeout=CACHE_TTL_SECONDS)

    @classmethod
    def load(cls, session_id: str) -> FlowSimulatorSession | None:
        key = f"{CACHE_PREFIX}{session_id}"
        data = cache.get(key)
        if not data or not isinstance(data, dict):
            return None
        return cls.from_dict(data)

    def start(self) -> dict[str, Any]:
        """Start the simulation from the entry node."""
        graph = Graph(self.graph)
        entry_id = graph.entry_node_id()
        if not entry_id:
            self.status = "failed"
            self.trace.append({
                "type": "error",
                "timestamp": self.virtual_time,
                "message": "O fluxo não possui um nó inicial válido.",
            })
            self.save()
            return self.to_dict()

        self.current_node_id = entry_id
        self.visited_nodes = []

        workspace = Workspace.objects.filter(pk=self.workspace_id).first()
        flow = Flow.objects.for_workspace(workspace).filter(pk=self.flow_id).first() if workspace else None
        if flow:
            from apps.channels.models import ChannelConnection
            from apps.channels.providers.instagram import get_flow_follower_settings
            conn = ChannelConnection.objects.for_workspace(workspace).filter(platform="instagram").first() if workspace else None
            settings = get_flow_follower_settings(flow, conn)
            if settings.require_follower:
                status = self.virtual_contact.get("follower_status", "follows")
                if status == "does_not_follow" or self.virtual_contact.get("is_follower") is False:
                    self.status = "waiting_follower"
                    text = settings.non_follower_message or "Primeiro você precisa me seguir para receber o acesso! Siga nosso perfil e clique no botão abaixo para continuar."
                    btn1 = settings.follow_button_label or "Seguir no Instagram"
                    btn2 = settings.confirm_button_label or "Já estou seguindo ✅"
                    self.trace = [
                        {
                            "type": "system",
                            "kind": "start",
                            "timestamp": self.virtual_time,
                            "message": "Simulação iniciada (exigência de seguidor ativa).",
                        },
                        {
                            "type": "bot_message",
                            "timestamp": self.virtual_time,
                            "node_id": "follower_gate",
                            "blocks": [{"type": "text", "text": text}],
                            "buttons": [
                                {"id": "btn_follow", "label": btn1, "action": "url", "url": "https://www.instagram.com/"},
                                {"id": f"ig_follow_check:flow:{self.flow_id}", "label": btn2, "action": "postback", "url": ""},
                            ],
                            "quick_replies": [],
                        },
                    ]
                    self.save()
                    return self.to_dict()
                elif status == "error":
                    self.trace.append({
                        "type": "system",
                        "kind": "warning",
                        "timestamp": self.virtual_time,
                        "message": "Erro simulado na API da Meta ao verificar seguidor. Fluxo liberado para evitar bloqueio indevido.",
                    })

        self.current_node_id = entry_id
        self.status = "running"
        self.visited_nodes = []
        self.trace = [{
            "type": "system",
            "kind": "start",
            "timestamp": self.virtual_time,
            "message": "Simulação iniciada.",
        }]
        self.step_loop()
        self.save()
        return self.to_dict()

    def step_loop(self) -> None:
        """Run through nodes until pause or end."""
        graph = Graph(self.graph)
        workspace = Workspace.objects.filter(pk=self.workspace_id).first()
        flow = Flow.objects.for_workspace(workspace).filter(pk=self.flow_id).first() if workspace else None
        step_budget = 40  # loop protection

        curr_virtual_dt = self.get_virtual_datetime()

        while self.status == "running" and self.current_node_id:
            step_budget -= 1
            if step_budget <= 0:
                self.status = "failed"
                self.trace.append({
                    "type": "error",
                    "timestamp": self.virtual_time,
                    "node_id": self.current_node_id,
                    "message": "Limite de passos sem pausa atingido (loop infinito detectado).",
                })
                break

            node_id = self.current_node_id
            if node_id not in self.visited_nodes:
                self.visited_nodes.append(node_id)

            node = graph.node(node_id)
            if node is None:
                self.status = "failed"
                self.trace.append({
                    "type": "error",
                    "timestamp": self.virtual_time,
                    "node_id": node_id,
                    "message": f"Nó '{node_id}' não encontrado no grafo.",
                })
                break

            node_type = graph.node_type(node_id)

            # Special case for action node: execute in-memory side effects without DB
            if node_type == "action":
                self._simulate_action_node(graph.config(node_id), node_id)
                target = graph.target(node_id, "default")
                if target:
                    self.current_node_id = target
                    continue
                else:
                    self.status = "completed"
                    self.trace.append({
                        "type": "system",
                        "kind": "completed",
                        "timestamp": self.virtual_time,
                        "message": "Fluxo concluído após ações.",
                    })
                    break

            # Intercept external request to prevent live network calls
            if node_type == "external_request":
                cfg = graph.config(node_id) or {}
                method = str(cfg.get("method") or "GET").upper()
                raw_url = str(cfg.get("url") or "")
                contact_for_render = SimulatedContact(self.virtual_contact, workspace=workspace)
                rendered_url = render(raw_url, context_for(contact_for_render, self.variables), mode="url")

                mappings = cfg.get("response_mappings") or []
                for m in mappings:
                    if isinstance(m, dict):
                        target = str(m.get("target") or m.get("variable") or "").strip()
                        if not target:
                            continue
                        target_type = str(m.get("target_type") or "variable").strip().lower()
                        mock_value = f"mock_{target}"
                        if target_type == "custom_field":
                            self.virtual_contact.setdefault("custom_fields", {})[target] = mock_value
                        else:
                            self.variables[target] = mock_value

                self.trace.append({
                    "type": "system",
                    "kind": "external_request",
                    "timestamp": self.virtual_time,
                    "node_id": node_id,
                    "message": f"Requisição simulada: {method} {rendered_url} (interceptada com sucesso - sem tráfego externo).",
                })
                target = graph.target(node_id, "default")
                if target:
                    self.current_node_id = target
                    continue
                else:
                    self.status = "completed"
                    self.trace.append({
                        "type": "system",
                        "kind": "completed",
                        "timestamp": self.virtual_time,
                        "message": "Fluxo concluído após requisição simulada.",
                    })
                    break

            # Intercept send_sms / send_email to prevent real outbound sends
            if node_type in ("send_sms", "send_email"):
                self.trace.append({
                    "type": "system",
                    "kind": node_type,
                    "timestamp": self.virtual_time,
                    "node_id": node_id,
                    "message": f"Envio simulado ({node_type}) interceptado com sucesso.",
                })
                target = graph.target(node_id, "default")
                if target:
                    self.current_node_id = target
                    continue
                else:
                    self.status = "completed"
                    self.trace.append({
                        "type": "system",
                        "kind": "completed",
                        "timestamp": self.virtual_time,
                        "message": f"Fluxo concluído após {node_type} simulado.",
                    })
                    break

            node_class = node_class_for(node_type)
            if node_class is None:
                self.status = "failed"
                self.trace.append({
                    "type": "error",
                    "timestamp": self.virtual_time,
                    "node_id": node_id,
                    "message": f"Nenhum executor registrado para nós do tipo '{node_type}'.",
                })
                break

            contact = SimulatedContact(self.virtual_contact, workspace=workspace)
            connection = SimulatedChannelConnection(workspace=workspace) if workspace else None
            execution = SimulatedExecution(self, workspace, flow, contact, connection)

            ctx = NodeContext(
                execution=execution,
                graph=graph,
                node_id=node_id,
                node_type=node_type,
                config=graph.config(node_id),
                variables=self.variables,
                now=curr_virtual_dt,
            )

            try:
                result: StepResult = node_class().execute(ctx)
            except Exception as exc:
                logger.exception("Simulation error executing node %s: %s", node_id, exc)
                self.status = "failed"
                self.trace.append({
                    "type": "error",
                    "timestamp": self.virtual_time,
                    "node_id": node_id,
                    "message": f"Erro de execução: {exc}",
                })
                break

            # Sync updated variables from context
            self.variables = dict(ctx.variables)

            if isinstance(result, Continue):
                target = graph.target(node_id, result.handle)
                if target is None:
                    self.status = "completed"
                    self.trace.append({
                        "type": "system",
                        "kind": "completed",
                        "timestamp": self.virtual_time,
                        "message": "Conversa finalizada (fim do fluxo).",
                    })
                    break
                self.current_node_id = target
                continue

            elif isinstance(result, Wait):
                self.status = "waiting_reply"
                self.wait_config = dict(result.config)
                timeout_block = result.config.get("timeout")
                t_msg = f" (timeout: {timeout_block.get('run_at')})" if timeout_block else ""
                self.trace.append({
                    "type": "system",
                    "kind": "wait",
                    "timestamp": self.virtual_time,
                    "node_id": node_id,
                    "message": f"Aguardando resposta do usuário...{t_msg}",
                })
                break

            elif isinstance(result, Schedule):
                self.status = "waiting_delay"
                self.wait_config = {
                    "run_at": result.run_at.isoformat(),
                    "resume_handle": result.resume_handle,
                    **result.config,
                }
                self.trace.append({
                    "type": "system",
                    "kind": "delay",
                    "timestamp": self.virtual_time,
                    "node_id": node_id,
                    "message": f"Delay aguardando até {result.run_at.isoformat()}",
                })
                break

            elif isinstance(result, Fail):
                self.status = "failed"
                self.trace.append({
                    "type": "error",
                    "timestamp": self.virtual_time,
                    "node_id": node_id,
                    "message": result.error,
                })
                break

            elif isinstance(result, End):
                self.status = "completed"
                if result.start_next:
                    self.trace.append({
                        "type": "system",
                        "kind": "completed",
                        "timestamp": self.virtual_time,
                        "message": f"Transição para o fluxo {result.start_next.flow_id}.",
                    })
                else:
                    self.trace.append({
                        "type": "system",
                        "kind": "completed",
                        "timestamp": self.virtual_time,
                        "message": "Fluxo concluído com sucesso.",
                    })
                break

            else:
                self.status = "failed"
                self.trace.append({
                    "type": "error",
                    "timestamp": self.virtual_time,
                    "node_id": node_id,
                    "message": f"Resultado desconhecido: {type(result)}",
                })
                break

    def _simulate_action_node(self, config: dict[str, Any], node_id: str) -> None:
        """Simulate action verbs on virtual contact and variables."""
        actions = config.get("actions") or []
        for step in actions:
            if not isinstance(step, dict):
                continue
            verb = str(step.get("verb") or "")
            if verb == "add_tag":
                tag = str(step.get("tag") or "").strip()
                if tag:
                    tags = self.virtual_contact.setdefault("tags", [])
                    if tag not in tags:
                        tags.append(tag)
                    self.trace.append({
                        "type": "system",
                        "kind": "action",
                        "timestamp": self.virtual_time,
                        "node_id": node_id,
                        "message": f"Etiqueta adicionada: '{tag}'",
                    })
            elif verb == "remove_tag":
                tag = str(step.get("tag") or "").strip()
                tags = self.virtual_contact.get("tags") or []
                self.virtual_contact["tags"] = [t for t in tags if t != tag]
                self.trace.append({
                    "type": "system",
                    "kind": "action",
                    "timestamp": self.virtual_time,
                    "node_id": node_id,
                    "message": f"Etiqueta removida: '{tag}'",
                })
            elif verb == "set_field":
                field = str(step.get("field") or "").strip()
                val = step.get("value")
                rendered_val = render(val, context_for(SimulatedContact(self.virtual_contact), self.variables))
                if field in ("first_name", "last_name", "phone", "email"):
                    self.virtual_contact[field] = rendered_val
                else:
                    custom = self.virtual_contact.setdefault("custom_fields", {})
                    custom[field] = rendered_val
                self.trace.append({
                    "type": "system",
                    "kind": "action",
                    "timestamp": self.virtual_time,
                    "node_id": node_id,
                    "message": f"Campo '{field}' definido como: {rendered_val}",
                })
            elif verb == "clear_field":
                field = str(step.get("field") or "").strip()
                custom = self.virtual_contact.get("custom_fields") or {}
                custom.pop(field, None)
                self.trace.append({
                    "type": "system",
                    "kind": "action",
                    "timestamp": self.virtual_time,
                    "node_id": node_id,
                    "message": f"Campo '{field}' limpo.",
                })
            elif verb in ("open_conversation", "close_conversation"):
                self.trace.append({
                    "type": "system",
                    "kind": "action",
                    "timestamp": self.virtual_time,
                    "node_id": node_id,
                    "message": f"Conversa: {verb}",
                })
            elif verb == "assign_conversation":
                member = step.get("member")
                self.trace.append({
                    "type": "system",
                    "kind": "action",
                    "timestamp": self.virtual_time,
                    "node_id": node_id,
                    "message": f"Conversa atribuída ao membro {member}",
                })
            elif verb == "notify_members":
                text = str(step.get("text") or "")
                self.trace.append({
                    "type": "system",
                    "kind": "action",
                    "timestamp": self.virtual_time,
                    "node_id": node_id,
                    "message": f"Notificação para membros: {text}",
                })

    def send_reply(self, text: str = "", button_id: str = "") -> dict[str, Any]:
        """User sends a reply message or clicks an interactive button/list row."""
        if self.status == "waiting_follower" or (button_id and button_id.startswith("ig_follow_check:")):
            self.trace.append({
                "type": "user_reply",
                "timestamp": self.virtual_time,
                "text": text,
                "button_id": button_id,
            })
            status = self.virtual_contact.get("follower_status", "follows")
            if status == "does_not_follow" or self.virtual_contact.get("is_follower") is False:
                self.trace.append({
                    "type": "system",
                    "kind": "follower_check",
                    "timestamp": self.virtual_time,
                    "message": "Verificação de seguidor: usuário ainda não segue o perfil comercial. Acesso bloqueado.",
                })
                workspace = Workspace.objects.filter(pk=self.workspace_id).first()
                flow = Flow.objects.for_workspace(workspace).filter(pk=self.flow_id).first() if workspace else None
                conn = ChannelConnection.objects.for_workspace(workspace).filter(platform="instagram").first() if workspace else None
                from apps.channels.providers.instagram import get_flow_follower_settings
                settings = get_flow_follower_settings(flow, conn)
                text = settings.non_follower_message or "Primeiro você precisa me seguir para receber o acesso! Siga nosso perfil e clique no botão abaixo para continuar."
                btn1 = settings.follow_button_label or "Seguir no Instagram"
                btn2 = settings.confirm_button_label or "Já estou seguindo ✅"
                self.trace.append({
                    "type": "bot_message",
                    "timestamp": self.virtual_time,
                    "node_id": "follower_gate",
                    "blocks": [{"type": "text", "text": text}],
                    "buttons": [
                        {"id": "btn_follow", "label": btn1, "action": "url", "url": "https://www.instagram.com/"},
                        {"id": f"ig_follow_check:flow:{self.flow_id}", "label": btn2, "action": "postback", "url": ""},
                    ],
                    "quick_replies": [],
                })
                self.save()
                return self.to_dict()
            else:
                self.status = "running"
                self.trace.append({
                    "type": "system",
                    "kind": "follower_check",
                    "timestamp": self.virtual_time,
                    "message": "Seguidor verificado com sucesso! Liberando acesso ao fluxo.",
                })
                self.step_loop()
                self.save()
                return self.to_dict()

        if self.status != "waiting_reply":
            return {"error": "A conversa não está aguardando resposta no momento."}

        graph = Graph(self.graph)
        wait_type = self.wait_config.get("type")

        # Record user reply in trace
        self.trace.append({
            "type": "user_reply",
            "timestamp": self.virtual_time,
            "text": text,
            "button_id": button_id,
        })

        if wait_type == WAIT_BUTTONS:
            handles: dict[str, str] = self.wait_config.get("handles") or {}
            labels: dict[str, str] = self.wait_config.get("labels") or {}

            chosen_handle: str | None = None
            if button_id:
                if button_id in handles:
                    chosen_handle = handles[button_id]
                elif f"btn:{button_id}" in handles.values():
                    chosen_handle = f"btn:{button_id}"
                elif f"qr:{button_id}" in handles.values():
                    chosen_handle = f"qr:{button_id}"

            if chosen_handle is None and text:
                norm_text = text.strip().casefold()
                if norm_text and norm_text in labels:
                    matched_id = str(labels[norm_text])
                    if matched_id in handles:
                        chosen_handle = str(handles[matched_id])
                    else:
                        chosen_handle = f"btn:{matched_id}"
                else:
                    from apps.flows.triggers.keywords import strip_accents
                    norm_unaccented = strip_accents(norm_text)
                    for lbl_key, lbl_val in labels.items():
                        if strip_accents(str(lbl_key).strip().casefold()) == norm_unaccented:
                            m_id = str(lbl_val)
                            chosen_handle = str(handles.get(m_id, f"btn:{m_id}"))
                            break

            if chosen_handle:
                target = graph.target(self.current_node_id or "", chosen_handle)
                if target:
                    self.current_node_id = target
                    self.status = "running"
                    self.wait_config = {}
                    self.step_loop()
                else:
                    self.status = "completed"
                    self.trace.append({
                        "type": "system",
                        "kind": "completed",
                        "timestamp": self.virtual_time,
                        "message": f"Fluxo concluído (sem próxima etapa para '{chosen_handle}').",
                    })
            else:
                # Unmatched button choice; check retries
                retry = self.wait_config.get("retry")
                if isinstance(retry, dict):
                    count = int(retry.get("count") or 0)
                    limit = int(retry.get("max") or 0)
                    if count < limit:
                        retry["count"] = count + 1
                        self.wait_config["retry"] = retry
                        prompt = str(retry.get("text") or "Opção inválida. Por favor, escolha uma das opções.")
                        self.trace.append({
                            "type": "bot_message",
                            "timestamp": self.virtual_time,
                            "node_id": self.current_node_id or "",
                            "blocks": [{"type": "text", "text": prompt}],
                            "buttons": [],
                            "quick_replies": [],
                            "is_retry": True,
                        })
                        self.save()
                        return self.to_dict()

                self.trace.append({
                    "type": "system",
                    "kind": "unmatched",
                    "timestamp": self.virtual_time,
                    "message": "Resposta não corresponde a nenhuma opção. Aguardando nova resposta ou timeout.",
                })

        elif wait_type == WAIT_DATA_COLLECTION:
            reply_type = str(self.wait_config.get("reply_type") or "text")
            try:
                norm_val = normalise_answer(text, reply_type)
                target = self.wait_config.get("target") or {}
                t_type = target.get("type")
                t_name = target.get("name") or target.get("key")

                if t_type == "custom_field" and t_name:
                    self.virtual_contact.setdefault("custom_fields", {})[t_name] = norm_val
                elif t_type in ("system", "system_field") and t_name:
                    self.virtual_contact[t_name] = norm_val
                elif t_type == "variable" and t_name:
                    self.variables[t_name] = norm_val

                # Also update virtual contact directly for email/phone reply types per SPEC §11.8
                if reply_type == "email":
                    self.virtual_contact["email"] = norm_val
                elif reply_type == "phone":
                    self.virtual_contact["phone"] = norm_val

                target_node = graph.target(self.current_node_id or "", "default")
                if target_node:
                    self.current_node_id = target_node
                    self.status = "running"
                    self.wait_config = {}
                    self.step_loop()
                else:
                    self.status = "completed"
                    self.trace.append({
                        "type": "system",
                        "kind": "completed",
                        "timestamp": self.virtual_time,
                        "message": "Fluxo concluído.",
                    })
            except InvalidAnswerError:
                retry = self.wait_config.get("retry") or {}
                count = int(retry.get("count") or 0)
                limit = int(retry.get("max") or 3)
                if count < limit:
                    retry["count"] = count + 1
                    self.wait_config["retry"] = retry
                    invalid_prompt = retry.get("text") or "Resposta inválida. Por favor, tente novamente."
                    self.trace.append({
                        "type": "bot_message",
                        "timestamp": self.virtual_time,
                        "node_id": self.current_node_id or "",
                        "blocks": [{"type": "text", "text": invalid_prompt}],
                        "buttons": [],
                        "quick_replies": [],
                        "is_retry": True,
                    })
                else:
                    # Per SPEC §9.3 & waits.py: when retries are spent, remain waiting until timeout
                    self.trace.append({
                        "type": "system",
                        "kind": "unmatched",
                        "timestamp": self.virtual_time,
                        "message": "Tentativas de resposta esgotadas. Aguardando até o tempo limite (timeout).",
                    })

        self.save()
        return self.to_dict()

    def advance_time(self, seconds: int = 3600, force_timeout: bool = False) -> dict[str, Any]:
        """Advance the virtual clock, triggering delay resumption or timeout branches."""
        graph = Graph(self.graph)
        try:
            curr_dt = datetime.fromisoformat(self.virtual_time)
        except Exception:
            curr_dt = timezone.now()

        new_dt = curr_dt + timedelta(seconds=max(seconds, 0))
        self.virtual_time = new_dt.isoformat()

        if self.status == "waiting_delay":
            scheduled_str = self.wait_config.get("run_at")
            should_resume = force_timeout
            if not should_resume and scheduled_str:
                try:
                    sched_dt = datetime.fromisoformat(scheduled_str)
                    if new_dt >= sched_dt:
                        should_resume = True
                except Exception:
                    should_resume = True

            if should_resume:
                resume_handle = str(self.wait_config.get("resume_handle") or "default")
                target = graph.target(self.current_node_id or "", resume_handle)
                if target:
                    self.current_node_id = target
                    self.status = "running"
                    self.wait_config = {}
                    dur_str = f"{seconds}s" if seconds < 60 else f"{seconds // 60} min"
                    self.trace.append({
                        "type": "system",
                        "kind": "advance",
                        "timestamp": self.virtual_time,
                        "message": f"Delay avançado ({dur_str}). Continuando fluxo...",
                    })
                    self.step_loop()
                else:
                    self.status = "completed"
                    self.trace.append({
                        "type": "system",
                        "kind": "completed",
                        "timestamp": self.virtual_time,
                        "message": "Delay concluído. Fluxo finalizado.",
                    })
            else:
                self.trace.append({
                    "type": "system",
                    "kind": "advance",
                    "timestamp": self.virtual_time,
                    "message": f"Tempo avançado em +{seconds // 60}m. Ainda aguardando delay até {scheduled_str}.",
                })

        elif self.status == "waiting_reply":
            timeout_block = self.wait_config.get("timeout")
            should_timeout = force_timeout
            if not should_timeout and isinstance(timeout_block, dict):
                deadline_str = timeout_block.get("run_at")
                if deadline_str:
                    try:
                        d_dt = datetime.fromisoformat(deadline_str)
                        if new_dt >= d_dt:
                            should_timeout = True
                    except Exception:
                        pass

            if should_timeout:
                target = graph.target(self.current_node_id or "", "timeout")
                if target:
                    self.current_node_id = target
                    self.status = "running"
                    self.wait_config = {}
                    self.trace.append({
                        "type": "system",
                        "kind": "timeout",
                        "timestamp": self.virtual_time,
                        "message": "Tempo limite esgotado. Seguindo pelo caminho 'timeout'.",
                    })
                    self.step_loop()
                else:
                    self.status = "completed"
                    self.trace.append({
                        "type": "system",
                        "kind": "completed",
                        "timestamp": self.virtual_time,
                        "message": "Timeout disparado, sem conexão configurada no handle 'timeout'. Conversa finalizada.",
                    })
            else:
                self.trace.append({
                    "type": "system",
                    "kind": "advance",
                    "timestamp": self.virtual_time,
                    "message": f"Tempo avançado em +{seconds // 60}m. Ainda aguardando resposta.",
                })

        self.save()
        return self.to_dict()

    def reset(self) -> dict[str, Any]:
        """Reset the conversation simulation back to beginning."""
        self.virtual_contact = copy.deepcopy(self.initial_contact)
        self.variables = copy.deepcopy(self.initial_variables)
        self.virtual_time = timezone.now().isoformat()
        self.status = "idle"
        self.current_node_id = None
        self.wait_config = {}
        self.visited_nodes = []
        self.trace = []
        return self.start()
