"""Views for Central Operacional (Operations Dashboard)."""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from apps.api.models import InboundLeadWebhook, LeadWebhookLog, OutboundWebhook, WebhookDelivery
from apps.channels.models import ChannelConnection, WebhookEventLog
from apps.members.decorators import require_permission
from apps.members.requests import WorkspaceRequest
from apps.operations.services import (
    bulk_retry_failed_actions,
    cancel_scheduled_action,
    get_failures_summary,
    get_operations_overview,
    get_queue_actions,
    retry_scheduled_action,
    test_channel_connection,
)
from apps.queueing.models import ActionStatus, ActionType, ScheduledAction


@login_required
@require_permission("manage_workspace_settings")
@require_GET
def dashboard(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """Operations center dashboard: queues, failures, disconnected channels, and webhooks."""
    tab = request.GET.get("tab", "overview")

    overview = get_operations_overview(request.workspace)
    context: dict[str, Any] = {
        "tab": tab,
        "overview": overview,
    }

    if tab == "queues":
        status_filter = request.GET.get("status", "all")
        type_filter = request.GET.get("type", "all")
        q = request.GET.get("q", "").strip()
        actions = get_queue_actions(
            request.workspace,
            status=status_filter,
            action_type=type_filter,
            q=q,
            limit=50,
        )
        # Distinct action types in this workspace for the filter dropdown
        available_types = list(
            ScheduledAction.objects.for_workspace(request.workspace)
            .values_list("type", flat=True)
            .distinct()
        )
        context.update(
            {
                "actions": actions,
                "status_filter": status_filter,
                "type_filter": type_filter,
                "q": q,
                "available_types": sorted(available_types),
                "action_statuses": ActionStatus.choices,
            }
        )

    elif tab == "failures":
        failures = get_failures_summary(request.workspace)
        context.update(failures)

    elif tab == "channels":
        connections = (
            ChannelConnection.objects.for_workspace(request.workspace)
            .select_related("evolution_session", "instagram_settings")
            .order_by("platform", "display_name")
        )
        context.update({"connections": connections})

    elif tab == "webhooks":
        # Inbound and outbound webhooks
        inbound_events = (
            WebhookEventLog.objects.filter(connection__workspace=request.workspace)
            .select_related("connection")
            .order_by("-received_at")[:40]
        )
        lead_logs = (
            LeadWebhookLog.objects.filter(webhook__workspace=request.workspace)
            .select_related("webhook", "contact", "execution")
            .order_by("-received_at")[:40]
        )
        outbound_deliveries = (
            WebhookDelivery.objects.filter(webhook__workspace=request.workspace)
            .select_related("webhook")
            .order_by("-created_at")[:40]
        )
        context.update(
            {
                "inbound_events": inbound_events,
                "lead_logs": lead_logs,
                "outbound_deliveries": outbound_deliveries,
            }
        )

    return render(request, "operations/dashboard.html", context)


@login_required
@require_permission("manage_workspace_settings")
@require_POST
def action_retry(request: WorkspaceRequest, workspace_id: str, action_id: str) -> HttpResponse:
    """Reprocess a failed or cancelled queue action with duplication protection."""
    success = retry_scheduled_action(request.workspace, action_id)
    if success:
        messages.success(request, "Tarefa reprocessada com sucesso com proteção contra duplicidade.")
    else:
        messages.warning(request, "A tarefa não pôde ser reprocessada ou já estava em execução.")

    # HTMX response support
    if request.headers.get("HX-Request"):
        action = ScheduledAction.objects.for_workspace(request.workspace).filter(pk=action_id).first()
        return render(request, "operations/_action_row.html", {"action": action})

    return redirect(f"/w/{workspace_id}/operations/?tab=queues")


@login_required
@require_permission("manage_workspace_settings")
@require_POST
def action_cancel(request: WorkspaceRequest, workspace_id: str, action_id: str) -> HttpResponse:
    """Cancel a pending action in the queue."""
    success = cancel_scheduled_action(request.workspace, action_id)
    if success:
        messages.info(request, "Tarefa cancelada.")
    else:
        messages.warning(request, "Apenas tarefas pendentes podem ser canceladas.")

    if request.headers.get("HX-Request"):
        action = ScheduledAction.objects.for_workspace(request.workspace).filter(pk=action_id).first()
        return render(request, "operations/_action_row.html", {"action": action})

    return redirect(f"/w/{workspace_id}/operations/?tab=queues")


@login_required
@require_permission("manage_workspace_settings")
@require_POST
def bulk_retry(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """Bulk retry failed actions in the workspace."""
    succeeded, skipped = bulk_retry_failed_actions(request.workspace)
    if succeeded > 0:
        messages.success(
            request,
            f"{succeeded} tarefas foram colocadas em fila para reprocessamento seguro ({skipped} ignoradas).",
        )
    else:
        messages.info(request, "Nenhuma tarefa com falha precisava de reprocessamento.")

    return redirect(f"/w/{workspace_id}/operations/?tab=failures")


@login_required
@require_permission("manage_workspace_settings")
@require_GET
def action_detail(request: WorkspaceRequest, workspace_id: str, action_id: str) -> HttpResponse:
    """Modal or partial showing full details, payload and error stack of an action."""
    action = get_object_or_404(ScheduledAction.objects.for_workspace(request.workspace), pk=action_id)
    payload_pretty = json.dumps(action.payload, indent=2, ensure_ascii=False) if action.payload else "{}"

    return render(
        request,
        "operations/_action_detail_modal.html",
        {
            "action": action,
            "payload_pretty": payload_pretty,
        },
    )


@login_required
@require_permission("manage_workspace_settings")
@require_POST
def channel_test(request: WorkspaceRequest, workspace_id: str, connection_id: str) -> HttpResponse:
    """Trigger live health check and sync for a channel connection."""
    res = test_channel_connection(request.workspace, connection_id)
    if res.get("success"):
        messages.success(request, f"Diagnóstico: {res.get('message')}")
    else:
        messages.error(request, f"Falha no canal: {res.get('message')}")

    if request.headers.get("HX-Request"):
        conn = ChannelConnection.objects.for_workspace(request.workspace).filter(pk=connection_id).first()
        return render(request, "operations/_channel_card.html", {"conn": conn})

    return redirect(f"/w/{workspace_id}/operations/?tab=channels")
