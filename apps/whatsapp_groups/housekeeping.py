import logging

from apps.queueing.housekeeping import register_housekeeping_job
from apps.whatsapp_groups.services import process_scheduled_broadcasts

logger = logging.getLogger(__name__)


@register_housekeeping_job("process_scheduled_group_broadcasts")
def process_scheduled_group_broadcasts() -> str | None:
    """Sweep for any scheduled group broadcasts that were due but not yet processed."""
    count = process_scheduled_broadcasts()
    if count:
        return f"processed {count} scheduled group broadcast(s)"
    return None
