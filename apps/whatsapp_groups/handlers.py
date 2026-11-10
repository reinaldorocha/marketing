import logging
from datetime import timedelta
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.queueing.registry import register_handler, schedule
from apps.whatsapp_groups.models import BroadcastStatus, GroupBroadcast
from apps.whatsapp_groups.services import (
    claim_group_broadcast_for_sending,
    deliver_single_group,
    mark_broadcast_failed_committed,
    resolve_target_groups,
    set_broadcast_completed_committed,
    set_broadcast_total_groups_committed,
)

logger = logging.getLogger(__name__)


@register_handler("group_broadcast_send")
def handle_group_broadcast_send(payload: dict, action: Any) -> None:
    """Execute a WhatsApp group broadcast.

    Supports two execution modes:
    1. Fanout mode (payload without 'group_jid'):
       Atomically claims the broadcast with execution ownership, expands the
       target groups, sets total_groups_count, and schedules separate delivery
       tasks per recipient group. This guarantees isolation between recipients.
    2. Single-recipient mode (payload with 'group_jid'):
       Delivers to one recipient group with durable checkpointing. If sending fails,
       only this specific group retries, while already delivered groups remain committed.
    """
    broadcast_id = payload.get("broadcast_id")
    if not broadcast_id:
        return

    group_jid = payload.get("group_jid")

    # Mode 2: Per-recipient delivery task
    if group_jid:
        deliver_single_group(broadcast_id, group_jid, action)
        return

    # Mode 1: Campaign fanout
    claim_owner = f"worker:{action.pk}" if hasattr(action, "pk") and action.pk else ""
    broadcast = claim_group_broadcast_for_sending(broadcast_id, claim_owner=claim_owner, allow_resume=True)
    if not broadcast:
        # Already claimed by another process or no longer scheduled
        return

    if broadcast.status == BroadcastStatus.CANCELLED:
        return

    try:
        target_groups = resolve_target_groups(broadcast)
        total_groups = len(target_groups)
        broadcast.total_groups_count = total_groups

        if total_groups == 0:
            set_broadcast_completed_committed(broadcast.id)
            return

        set_broadcast_total_groups_committed(broadcast.id, total_groups)

        delay = min(max(broadcast.delay_seconds, 0), 15)
        now = timezone.now()

        for idx, group in enumerate(target_groups):
            schedule(
                "group_broadcast_send",
                run_at=now + timedelta(seconds=idx * delay),
                payload={"broadcast_id": str(broadcast.id), "group_jid": group.jid},
                workspace=broadcast.workspace,
                idempotency_key=f"group_broadcast:{broadcast.id}:{group.jid}",
            )
    except Exception as exc:
        logger.error("Error executing group broadcast fanout %s: %s", broadcast.id, exc)
        # If this action reached its max attempts, mark as FAILED in a way that survives rollback
        if hasattr(action, "attempts") and hasattr(action, "max_attempts") and action.attempts >= action.max_attempts:
            try:
                mark_broadcast_failed_committed(broadcast.id)
            except Exception as e:
                logger.error("Failed to commit final FAILED status for broadcast %s: %s", broadcast.id, e)
        raise
