import logging

from apps.broadcasts.models import BroadcastStatus
from apps.queueing.registry import register_handler
from apps.whatsapp_groups.models import GroupBroadcast
from apps.whatsapp_groups.services import claim_group_broadcast_for_sending, dispatch_group_broadcast

logger = logging.getLogger(__name__)


@register_handler("group_broadcast_send")
def handle_group_broadcast_send(payload: dict, action) -> None:
    """Execute a scheduled WhatsApp group broadcast when its run_at time arrives."""
    broadcast_id = payload.get("broadcast_id")
    if not broadcast_id:
        return

    broadcast = claim_group_broadcast_for_sending(broadcast_id, allow_resume=True)
    if not broadcast:
        # Already claimed or no longer scheduled
        return

    try:
        dispatch_group_broadcast(broadcast)
    except Exception as exc:
        logger.error("Error executing scheduled group broadcast %s: %s", broadcast.id, exc)
        # If this action reached its max attempts, mark as FAILED so it doesn't get stuck in SENDING
        if hasattr(action, "attempts") and hasattr(action, "max_attempts") and action.attempts >= action.max_attempts:
            try:
                broadcast.refresh_from_db()
                broadcast.status = BroadcastStatus.FAILED
                broadcast.save(update_fields=["status", "updated_at"])
            except Exception:
                pass
        raise
