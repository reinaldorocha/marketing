"""Operational control plane: queues, failures, channel health, and webhook diagnostics."""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from apps.api.models import DeliveryStatus, InboundLeadWebhook, LeadWebhookLog, OutboundWebhook, WebhookDelivery
from apps.broadcasts.models import Broadcast, BroadcastRecipient, BroadcastStatus, RecipientStatus
from apps.channels.models import ChannelConnection, ConnectionStatus, EvolutionSession, WebhookEventLog, WebhookEventStatus
from apps.publishing.models import ContentPost, PostStatus
from apps.queueing.models import ActionStatus, ActionType, ScheduledAction
from apps.workspaces.models import Workspace

logger = logging.getLogger(__name__)


def get_operations_overview(workspace: Workspace) -> dict[str, Any]:
    """Aggregate high-level operational metrics and health indicators for the workspace."""
    now = timezone.now()
    since_24h = now - timedelta(hours=24)
    stuck_threshold = now - timedelta(minutes=5)

    # 1. Queue Metrics
    queue_qs = ScheduledAction.objects.for_workspace(workspace)
    pending_count = queue_qs.filter(status=ActionStatus.PENDING).count()
    running_count = queue_qs.filter(status=ActionStatus.RUNNING).count()
    failed_count = queue_qs.filter(status=ActionStatus.FAILED).count()
    done_recent_count = queue_qs.filter(status=ActionStatus.DONE, updated_at__gte=since_24h).count()
    delayed_count = queue_qs.filter(status=ActionStatus.PENDING, run_at__lt=stuck_threshold).count()

    # 2. Channel Health
    channels_qs = ChannelConnection.objects.for_workspace(workspace)
    total_channels = channels_qs.count()
    active_channels = channels_qs.filter(status=ConnectionStatus.ACTIVE).count()
    disconnected_channels = channels_qs.exclude(status=ConnectionStatus.ACTIVE)
    disconnected_channels_count = disconnected_channels.count()

    # Check WhatsApp Evolution instances not in 'open' state
    evolution_unhealthy = (
        EvolutionSession.objects.filter(connection__workspace=workspace)
        .exclude(state="open")
        .select_related("connection")
    )
    evolution_unhealthy_count = evolution_unhealthy.count()

    # 3. Webhook Health
    inbound_logs_24h = WebhookEventLog.objects.filter(connection__workspace=workspace, received_at__gte=since_24h)
    inbound_total = inbound_logs_24h.count()
    inbound_failed = inbound_logs_24h.filter(status=WebhookEventStatus.FAILED).count()
    inbound_duplicates = inbound_logs_24h.filter(status=WebhookEventStatus.SKIPPED_DUPLICATE).count()

    lead_logs_24h = LeadWebhookLog.objects.filter(webhook__workspace=workspace, received_at__gte=since_24h)
    lead_total = lead_logs_24h.count()
    lead_failed = lead_logs_24h.filter(status="error").count()

    outbound_deliveries_24h = WebhookDelivery.objects.for_workspace(workspace).filter(created_at__gte=since_24h)
    outbound_total = outbound_deliveries_24h.count()
    outbound_failed = outbound_deliveries_24h.filter(status=DeliveryStatus.FAILED).count()

    # 4. Broadcasts & Group Campaigns Health
    stuck_broadcasts = Broadcast.objects.for_workspace(workspace).filter(
        status=BroadcastStatus.SENDING,
        updated_at__lt=now - timedelta(minutes=15),
    ).count()

    failed_posts_count = ContentPost.objects.for_workspace(workspace).filter(status=PostStatus.FAILED).count()

    has_critical_alerts = bool(
        delayed_count > 0
        or failed_count > 0
        or disconnected_channels_count > 0
        or evolution_unhealthy_count > 0
        or stuck_broadcasts > 0
        or lead_failed > 0
        or outbound_failed > 0
    )

    return {
        "queues": {
            "pending": pending_count,
            "running": running_count,
            "failed": failed_count,
            "done_recent": done_recent_count,
            "delayed": delayed_count,
        },
        "channels": {
            "total": total_channels,
            "active": active_channels,
            "disconnected_count": disconnected_channels_count,
            "disconnected_list": disconnected_channels[:10],
            "evolution_unhealthy_count": evolution_unhealthy_count,
            "evolution_unhealthy_list": evolution_unhealthy[:10],
        },
        "webhooks": {
            "inbound_total": inbound_total,
            "inbound_failed": inbound_failed,
            "inbound_duplicates": inbound_duplicates,
            "lead_total": lead_total,
            "lead_failed": lead_failed,
            "outbound_total": outbound_total,
            "outbound_failed": outbound_failed,
        },
        "campaigns": {
            "stuck_broadcasts": stuck_broadcasts,
            "failed_posts": failed_posts_count,
        },
        "has_critical_alerts": has_critical_alerts,
        "now": now,
    }


def get_queue_actions(
    workspace: Workspace,
    *,
    status: str = "all",
    action_type: str = "all",
    q: str = "",
    limit: int = 50,
) -> list[ScheduledAction]:
    """Retrieve filtered scheduled actions for this workspace."""
    qs = ScheduledAction.objects.for_workspace(workspace)

    if status != "all" and status in ActionStatus.values:
        qs = qs.filter(status=status)
    if action_type != "all" and action_type:
        qs = qs.filter(type=action_type)
    if q:
        # Search by ID or contact_id
        try:
            val_uuid = UUID(q)
            qs = qs.filter(id=val_uuid) | qs.filter(contact_id=val_uuid)
        except (ValueError, AttributeError):
            qs = qs.filter(last_error__icontains=q) | qs.filter(type__icontains=q)

    return list(qs.order_by("-run_at")[:limit])


def get_failures_summary(workspace: Workspace) -> dict[str, Any]:
    """Retrieve failed actions, webhooks, broadcasts, and posts for operational inspection."""
    # 1. Failed Queue Actions
    failed_actions = list(
        ScheduledAction.objects.for_workspace(workspace)
        .filter(status=ActionStatus.FAILED)
        .order_by("-updated_at")[:50]
    )

    # 2. Failed Inbound Webhooks
    failed_inbound = list(
        WebhookEventLog.objects.filter(
            connection__workspace=workspace,
            status=WebhookEventStatus.FAILED,
        )
        .select_related("connection")
        .order_by("-received_at")[:30]
    )

    # 3. Failed Inbound Lead Webhooks
    failed_leads = list(
        LeadWebhookLog.objects.filter(
            webhook__workspace=workspace,
            status="error",
        )
        .select_related("webhook", "contact")
        .order_by("-received_at")[:30]
    )

    # 4. Failed Outbound Webhooks
    failed_outbound = list(
        WebhookDelivery.objects.for_workspace(workspace).filter(
            status=DeliveryStatus.FAILED,
        )
        .select_related("webhook")
        .order_by("-created_at")[:30]
    )

    # 5. Failed Posts
    failed_posts = list(
        ContentPost.objects.for_workspace(workspace)
        .filter(status=PostStatus.FAILED)
        .select_related("connection")
        .order_by("-updated_at")[:30]
    )

    return {
        "failed_actions": failed_actions,
        "failed_inbound": failed_inbound,
        "failed_leads": failed_leads,
        "failed_outbound": failed_outbound,
        "failed_posts": failed_posts,
        "total_failures": len(failed_actions) + len(failed_inbound) + len(failed_leads) + len(failed_outbound) + len(failed_posts),
    }


def retry_scheduled_action(workspace: Workspace, action_id: str | UUID) -> bool:
    """Safely retry a failed or cancelled ScheduledAction with duplication protection.

    - Atomically locks the action with select_for_update().
    - Verifies the action is currently terminal (FAILED or CANCELLED) and belongs to the workspace.
    - Resets attempts to 0, run_at to now, and status to PENDING.
    - Preserves existing idempotency_key so duplicate queue rows cannot be spawned.
    """
    try:
        with transaction.atomic():
            locked = (
                ScheduledAction.objects.for_workspace(workspace)
                .select_for_update()
                .filter(pk=action_id)
                .first()
            )
            if not locked:
                logger.warning("retry_scheduled_action: action %s not found in workspace %s", action_id, workspace.pk)
                return False

            if locked.status not in (ActionStatus.FAILED, ActionStatus.CANCELLED):
                logger.info(
                    "retry_scheduled_action: action %s is in status %s; skipping to avoid duplicate execution",
                    action_id,
                    locked.status,
                )
                return False

            now = timezone.now()
            locked.status = ActionStatus.PENDING
            locked.run_at = now
            locked.attempts = 0
            locked.last_error = f"[Reprocessado manualmente em {now.strftime('%d/%m/%Y %H:%M:%S')}]"
            locked.save(update_fields=["status", "run_at", "attempts", "last_error", "updated_at"])

            logger.info("retry_scheduled_action: action %s re-queued as PENDING successfully", action_id)
            return True
    except Exception as exc:
        logger.exception("retry_scheduled_action: unexpected error retrying action %s: %s", action_id, exc)
        return False


def cancel_scheduled_action(workspace: Workspace, action_id: str | UUID) -> bool:
    """Cancel a pending action in the queue."""
    try:
        with transaction.atomic():
            locked = (
                ScheduledAction.objects.for_workspace(workspace)
                .select_for_update()
                .filter(pk=action_id)
                .first()
            )
            if not locked:
                return False

            if locked.status != ActionStatus.PENDING:
                return False

            locked.status = ActionStatus.CANCELLED
            locked.save(update_fields=["status", "updated_at"])
            return True
    except Exception as exc:
        logger.exception("cancel_scheduled_action: error cancelling action %s: %s", action_id, exc)
        return False


def bulk_retry_failed_actions(workspace: Workspace, action_ids: list[str] | None = None) -> tuple[int, int]:
    """Bulk retry failed scheduled actions with duplication protection.

    Returns (success_count, skipped_count).
    """
    qs = ScheduledAction.objects.for_workspace(workspace).filter(status=ActionStatus.FAILED)
    if action_ids:
        qs = qs.filter(pk__in=action_ids)

    candidate_ids = list(qs.values_list("pk", flat=True)[:100])
    succeeded = 0
    skipped = 0

    for aid in candidate_ids:
        if retry_scheduled_action(workspace, aid):
            succeeded += 1
        else:
            skipped += 1

    return succeeded, skipped


def test_channel_connection(workspace: Workspace, connection_id: str | UUID) -> dict[str, Any]:
    """Perform real-time diagnostics and status synchronization for a channel connection."""
    conn = ChannelConnection.objects.for_workspace(workspace).filter(pk=connection_id).first()
    if not conn:
        return {"success": False, "status": "not_found", "message": "Canal não encontrado."}

    # WhatsApp Evolution
    if conn.platform == "whatsapp_evolution" or hasattr(conn, "evolution_session"):
        from apps.channels.providers.whatsapp_evolution import WhatsAppEvolutionAdapter

        adapter = WhatsAppEvolutionAdapter()
        client = adapter.get_client(conn)

        if not client.api_url or not (client.api_key or client.instance_id):
            conn.status = ConnectionStatus.NEEDS_REAUTH
            conn.save(update_fields=["status"])
            return {
                "success": False,
                "status": "unconfigured",
                "message": "Instância Evolution GO não configurada (URL ou credenciais ausentes).",
            }

        session = getattr(conn, "evolution_session", None)
        if not session:
            session = EvolutionSession.objects.filter(connection=conn).first()

        try:
            res = client.get_status()
            state = "unknown"
            if isinstance(res, dict):
                inst = res.get("instance") if isinstance(res.get("instance"), dict) else {}
                st = res.get("state") or inst.get("state") or inst.get("status") or res.get("status") or ""
                if st:
                    state = str(st).lower()

            if state in ("open", "connected"):
                if session:
                    session.state = "open"
                    session.last_connected_at = timezone.now()
                    session.last_error = ""
                    session.save(update_fields=["state", "last_connected_at", "last_error"])
                conn.status = ConnectionStatus.ACTIVE
                conn.save(update_fields=["status"])
                return {
                    "success": True,
                    "status": "open",
                    "message": "Instância conectada e pronta (OPEN).",
                }
            else:
                if session:
                    session.state = state
                    session.last_disconnected_at = timezone.now()
                    session.last_error = f"Evolution retornou estado: {state}"
                    session.save(update_fields=["state", "last_disconnected_at", "last_error"])
                conn.status = ConnectionStatus.NEEDS_REAUTH
                conn.save(update_fields=["status"])
                return {
                    "success": False,
                    "status": state,
                    "message": f"Instância desconectada ou pendente: {state.upper()}",
                }
        except Exception as exc:
            if session:
                session.last_error = str(exc)
                session.save(update_fields=["last_error"])
            conn.status = ConnectionStatus.NEEDS_REAUTH
            conn.save(update_fields=["status"])
            return {
                "success": False,
                "status": "error",
                "message": f"Erro conectando à Evolution GO: {exc}",
            }

    # Instagram
    if conn.platform == "instagram":
        from apps.channels import instagram_oauth
        from apps.channels.providers.exceptions import APIError

        try:
            token = instagram_oauth.access_token(conn)
            if not token:
                conn.status = ConnectionStatus.NEEDS_REAUTH
                conn.save(update_fields=["status"])
                return {"success": False, "status": "needs_reauth", "message": "Token do Instagram ausente."}

            expires = instagram_oauth.token_expires_at(conn)
            if expires and expires <= timezone.now():
                conn.status = ConnectionStatus.NEEDS_REAUTH
                conn.save(update_fields=["status"])
                return {
                    "success": False,
                    "status": "needs_reauth",
                    "message": f"Token expirou em {expires.strftime('%d/%m/%Y %H:%M')}.",
                }

            # Actively verify token validity against Meta Graph API
            profile = instagram_oauth.account_profile(token)
            username = profile.get("username", "")

            conn.status = ConnectionStatus.ACTIVE
            conn.save(update_fields=["status"])
            return {
                "success": True,
                "status": "active",
                "message": f"Token verificado na Meta com sucesso (@{username}).",
            }
        except APIError as exc:
            conn.status = ConnectionStatus.NEEDS_REAUTH
            conn.save(update_fields=["status"])
            return {
                "success": False,
                "status": "needs_reauth",
                "message": f"Token recusado pela Meta: {exc}",
            }
        except Exception as exc:
            conn.status = ConnectionStatus.NEEDS_REAUTH
            conn.save(update_fields=["status"])
            return {"success": False, "status": "error", "message": f"Erro verificando conta Meta: {exc}"}

    return {"success": True, "status": conn.status, "message": f"Canal verificado: {conn.get_status_display()}"}
