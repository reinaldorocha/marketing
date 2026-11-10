"""Simulator view endpoints for Flow Builder."""

import json
from typing import Any
from uuid import uuid4

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.views.decorators.http import require_POST

from apps.common.shortcuts import get_scoped_object_or_404
from apps.flows import services
from apps.flows.models import Flow
from apps.flows.schema import MAX_GRAPH_BYTES, empty_graph
from apps.flows.simulator.engine import FlowSimulatorSession
from apps.members.decorators import require_workspace_role
from apps.members.requests import WorkspaceRequest
from apps.members.roles import WorkspaceRole

__all__ = [
    "simulator_advance",
    "simulator_reply",
    "simulator_reset",
    "simulator_start",
]

require_workspace_member = require_workspace_role(WorkspaceRole.VIEWER)
MAX_REQUEST_BYTES = MAX_GRAPH_BYTES + 8192


def _read_json(request: WorkspaceRequest) -> dict[str, Any] | JsonResponse:
    try:
        body = request.body
    except ValueError:
        return JsonResponse({"error": {"code": "bad_request", "message": "Invalid body"}}, status=400)
    if len(body) > MAX_REQUEST_BYTES:
        return JsonResponse({"error": {"code": "payload_too_large", "message": "Payload too large"}}, status=413)
    try:
        data = json.loads(body or b"{}")
    except Exception:
        return JsonResponse({"error": {"code": "invalid_json", "message": "Invalid JSON"}}, status=400)
    return data if isinstance(data, dict) else {}


@login_required
@require_workspace_member
@require_POST
def simulator_start(request: WorkspaceRequest, workspace_id: str, flow_id: str) -> HttpResponse:
    """Start a fresh simulation session for a flow draft."""
    flow = get_scoped_object_or_404(Flow, request.workspace, pk=flow_id)
    payload = _read_json(request)
    if isinstance(payload, JsonResponse):
        return payload

    graph = payload.get("graph")
    if not isinstance(graph, dict):
        draft = services.latest_version(flow)
        graph = draft.graph_json if draft else empty_graph()

    contact_data = payload.get("contact")
    variables_data = payload.get("variables")

    session_id = uuid4().hex
    session = FlowSimulatorSession(
        session_id=session_id,
        workspace_id=str(flow.workspace_id),
        flow_id=str(flow.pk),
        graph=graph,
        virtual_contact=contact_data if isinstance(contact_data, dict) else None,
        variables=variables_data if isinstance(variables_data, dict) else None,
        user_id=str(request.user.id),
    )
    result = session.start()
    return JsonResponse(result)


@login_required
@require_workspace_member
@require_POST
def simulator_reply(request: WorkspaceRequest, workspace_id: str, flow_id: str) -> HttpResponse:
    """Send a user message or click a button in an active simulation session."""
    payload = _read_json(request)
    if isinstance(payload, JsonResponse):
        return payload

    session_id = str(payload.get("session_id") or "")
    session = FlowSimulatorSession.load(session_id)
    if not session or session.flow_id != str(flow_id):
        return JsonResponse(
            {"error": {"code": "session_not_found", "message": "Sessão de simulação expirada ou não encontrada."}},
            status=404,
        )

    if session.workspace_id != str(request.workspace.id) or (session.user_id and session.user_id != str(request.user.id)):
        return JsonResponse(
            {"error": {"code": "forbidden", "message": "Acesso não autorizado a esta sessão de simulação."}},
            status=403,
        )

    text = str(payload.get("text") or "")
    button_id = str(payload.get("button_id") or "")
    result = session.send_reply(text=text, button_id=button_id)
    return JsonResponse(result)


@login_required
@require_workspace_member
@require_POST
def simulator_advance(request: WorkspaceRequest, workspace_id: str, flow_id: str) -> HttpResponse:
    """Advance simulation virtual time or trigger a timeout."""
    payload = _read_json(request)
    if isinstance(payload, JsonResponse):
        return payload

    session_id = str(payload.get("session_id") or "")
    session = FlowSimulatorSession.load(session_id)
    if not session or session.flow_id != str(flow_id):
        return JsonResponse(
            {"error": {"code": "session_not_found", "message": "Sessão de simulação expirada ou não encontrada."}},
            status=404,
        )

    if session.workspace_id != str(request.workspace.id) or (session.user_id and session.user_id != str(request.user.id)):
        return JsonResponse(
            {"error": {"code": "forbidden", "message": "Acesso não autorizado a esta sessão de simulação."}},
            status=403,
        )

    try:
        seconds = int(payload.get("seconds", 3600))
    except (ValueError, TypeError):
        seconds = 3600
    force_timeout = bool(payload.get("force_timeout", False))

    result = session.advance_time(seconds=seconds, force_timeout=force_timeout)
    return JsonResponse(result)


@login_required
@require_workspace_member
@require_POST
def simulator_reset(request: WorkspaceRequest, workspace_id: str, flow_id: str) -> HttpResponse:
    """Reset a simulation session to its initial state."""
    payload = _read_json(request)
    if isinstance(payload, JsonResponse):
        return payload

    session_id = str(payload.get("session_id") or "")
    session = FlowSimulatorSession.load(session_id)
    if not session or session.flow_id != str(flow_id):
        return JsonResponse(
            {"error": {"code": "session_not_found", "message": "Sessão de simulação expirada ou não encontrada."}},
            status=404,
        )

    if session.workspace_id != str(request.workspace.id) or (session.user_id and session.user_id != str(request.user.id)):
        return JsonResponse(
            {"error": {"code": "forbidden", "message": "Acesso não autorizado a esta sessão de simulação."}},
            status=403,
        )

    result = session.reset()
    return JsonResponse(result)
