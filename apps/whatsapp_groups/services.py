import logging
import threading
import time
import uuid
from typing import Any
from django.db import transaction
from django.utils import timezone

from apps.channels.models import ChannelConnection, ConnectionStatus
from apps.channels.providers.whatsapp_evolution import EvolutionClient, WhatsAppEvolutionAdapter
from apps.common.platforms import Platform
from apps.whatsapp_groups.models import (
    BroadcastMessageType,
    BroadcastStatus,
    BroadcastTargetMode,
    GroupBroadcast,
    WhatsAppGroup,
    WhatsAppGroupTag,
)

logger = logging.getLogger(__name__)


def get_evolution_client(connection: ChannelConnection) -> EvolutionClient:
    """Return an instantiated EvolutionClient for an active connection."""
    adapter = WhatsAppEvolutionAdapter()
    return adapter.get_client(connection)


def sync_groups_from_evolution(connection: ChannelConnection) -> int:
    """Fetch all groups from the Evolution GO instance and update the database."""
    if connection.platform != Platform.WHATSAPP_EVOLUTION.value:
        logger.warning("Connection %s is not a WhatsApp Evolution connection", connection.id)
        return 0

    client = get_evolution_client(connection)
    raw_groups = client.list_groups()
    logger.info("Found %d groups for connection %s", len(raw_groups), connection.id)

    synced_count = 0
    now = timezone.now()

    # Determine connection instance phone/JID to detect admin status
    session = getattr(connection, "evolution_session", None)
    raw_phone = session.phone_number if session else ""
    if not raw_phone and connection.external_id:
        raw_phone = connection.external_id
    conn_phone = "".join(ch for ch in str(raw_phone) if ch.isdigit())

    for item in raw_groups:
        jid = item.get("jid")
        if not jid or "@g.us" not in jid:
            continue

        name = item.get("name") or jid
        description = item.get("description") or ""
        owner_jid = item.get("owner") or ""
        is_announce = bool(item.get("is_announce", False))
        participants = item.get("participants") or []

        # Check if connected instance is admin
        is_admin = False
        if conn_phone and isinstance(participants, list):
            for p in participants:
                p_jid = str(p.get("JID") or p.get("jid") or "")
                p_digits = "".join(ch for ch in p_jid.split("@")[0].split(":")[0] if ch.isdigit())
                if p_digits and (conn_phone == p_digits or conn_phone in p_digits or p_digits in conn_phone):
                    is_admin = bool(p.get("IsAdmin") or p.get("isAdmin") or p.get("IsSuperAdmin") or p.get("isSuperAdmin"))
                    break

        group_obj, created = WhatsAppGroup.objects.for_workspace(connection.workspace).update_or_create(
            workspace=connection.workspace,
            connection=connection,
            jid=jid,
            defaults={
                "name": name,
                "description": description,
                "owner_jid": owner_jid,
                "is_announce": is_announce,
                "is_admin": is_admin,
                "participants_count": len(participants) if isinstance(participants, list) else 0,
                "participants_cache": participants if isinstance(participants, list) else [],
                "last_synced_at": now,
            },
        )
        synced_count += 1

    return synced_count


def create_group_on_evolution(
    connection: ChannelConnection,
    name: str,
    participants: list[str],
) -> WhatsAppGroup:
    """Form a new WhatsApp group on the Evolution GO instance."""
    client = get_evolution_client(connection)
    res = client.create_group(group_name=name, participants=participants)

    jid = ""
    if isinstance(res, dict):
        jid = str(res.get("gid") or res.get("jid") or res.get("groupJid") or res.get("id") or "")

    if not jid:
        jid = f"{int(time.time())}@g.us"

    group = WhatsAppGroup.objects.for_workspace(connection.workspace).create(
        workspace=connection.workspace,
        connection=connection,
        jid=jid,
        name=name,
        is_admin=True,
        participants_count=len(participants) + 1,
        participants_cache=[{"jid": p} for p in participants],
        last_synced_at=timezone.now(),
    )
    return group


def update_group_on_evolution(
    group: WhatsAppGroup,
    subject: str | None = None,
    description: str | None = None,
    announce: bool | None = None,
) -> bool:
    """Update group subject, description, or announcement settings on Evolution GO."""
    client = get_evolution_client(group.connection)
    updated = False

    if subject and subject.strip() != group.name:
        try:
            client.update_group_subject(group.jid, subject.strip())
            group.name = subject.strip()
            updated = True
        except Exception as exc:
            logger.warning("Could not update subject for group %s: %s", group.jid, exc)

    if description is not None and description != group.description:
        try:
            client.update_group_description(group.jid, description)
            group.description = description
            updated = True
        except Exception as exc:
            logger.warning("Could not update description for group %s: %s", group.jid, exc)

    if announce is not None and announce != group.is_announce:
        try:
            action = "announcement" if announce else "not_announcement"
            client.update_group_settings(group.jid, action)
            group.is_announce = announce
            updated = True
        except Exception as exc:
            logger.warning("Could not update settings for group %s: %s", group.jid, exc)

    if updated:
        group.save()
    return updated


def leave_group_on_evolution(group: WhatsAppGroup) -> bool:
    """Leave group on Evolution GO and remove from workspace database."""
    client = get_evolution_client(group.connection)
    try:
        client.leave_group(group.jid)
    except Exception as exc:
        logger.warning("Error leaving group %s: %s", group.jid, exc)
    group.delete()
    return True


def refresh_group_invite_link(group: WhatsAppGroup, revoke: bool = False) -> str:
    """Fetch or revoke/generate a new invite link for the group."""
    client = get_evolution_client(group.connection)
    code = ""
    try:
        if revoke:
            code = client.revoke_group_invite_code(group.jid)
        else:
            code = client.get_group_invite_code(group.jid)
    except Exception as exc:
        logger.warning("Error getting invite code for %s: %s", group.jid, exc)

    if code:
        group.invite_code = code
        group.save(update_fields=["invite_code", "updated_at"])
    return group.invite_link


def resolve_target_groups(broadcast: GroupBroadcast) -> list[WhatsAppGroup]:
    """Resolve and return the target WhatsAppGroup records for this broadcast."""
    connection = broadcast.connection
    target_groups_qs = WhatsAppGroup.objects.none()
    if broadcast.target_mode == BroadcastTargetMode.SELECTED:
        target_groups_qs = broadcast.target_groups.filter(
            workspace=broadcast.workspace,
            connection=connection,
        )
    elif broadcast.target_mode == BroadcastTargetMode.BY_TAG:
        target_groups_qs = (
            WhatsAppGroup.objects.for_workspace(broadcast.workspace)
            .filter(connection=connection, tags__in=broadcast.target_tags.all())
            .distinct()
        )
    elif broadcast.target_mode == BroadcastTargetMode.ALL:
        target_groups_qs = WhatsAppGroup.objects.for_workspace(broadcast.workspace).filter(
            connection=connection
        )
    return list(target_groups_qs)


def send_message_to_group(
    broadcast: GroupBroadcast,
    group: WhatsAppGroup | None,
    group_jid: str,
    client: EvolutionClient | None = None,
) -> None:
    """Send configured broadcast message payload to a specific group."""
    if client is None:
        client = get_evolution_client(broadcast.connection)

    mentions: list[str] = []
    if broadcast.mention_all and group and group.participants_cache:
        for p in group.participants_cache:
            p_jid = ""
            if isinstance(p, dict):
                p_jid = str(p.get("JID") or p.get("jid") or "")
            elif isinstance(p, str):
                p_jid = p
            if p_jid:
                mentions.append(p_jid)

    mtype = broadcast.message_type
    if mtype == BroadcastMessageType.TEXT:
        client.send_text(
            number=group_jid,
            text=broadcast.text,
            mentions=mentions if broadcast.mention_all else None,
        )
    elif mtype == BroadcastMessageType.MEDIA:
        client.send_media(
            number=group_jid,
            media_type=broadcast.media_type or "image",
            url=broadcast.media_url,
            caption=broadcast.media_caption,
            mentions=mentions if broadcast.mention_all else None,
        )
    elif mtype == BroadcastMessageType.POLL:
        client.send_poll(
            number=group_jid,
            question=broadcast.poll_question,
            options=broadcast.poll_options or ["Sim", "Não"],
            max_answers=broadcast.poll_max_answers or 1,
        )
    elif mtype == BroadcastMessageType.STICKER:
        client.send_sticker(
            number=group_jid,
            url=broadcast.sticker_url,
        )
    elif mtype == BroadcastMessageType.LOCATION:
        client.send_location(
            number=group_jid,
            latitude=broadcast.location_latitude or 0.0,
            longitude=broadcast.location_longitude or 0.0,
            name=broadcast.location_name,
            address=broadcast.location_address,
        )
    elif mtype == BroadcastMessageType.CONTACT:
        client.send_contact(
            number=group_jid,
            full_name=broadcast.contact_name,
            phone=broadcast.contact_phone,
            organization=broadcast.contact_organization,
        )


def _save_durable_checkpoint(
    broadcast_id: Any,
    delivery_logs: list[dict[str, Any]],
    sent_count: int,
    error_count: int,
) -> None:
    """Save broadcast delivery progress in a separate committed connection.

    Ensures that checkpoints survive even if the caller is wrapped inside
    a database transaction (such as a queue worker) that later rolls back.
    """
    def _run() -> None:
        from django.db import connection as db_connection
        try:
            now = timezone.now()
            (
                GroupBroadcast.objects.unscoped()
                .filter(pk=broadcast_id)
                .update(
                    delivery_logs=delivery_logs,
                    sent_count=sent_count,
                    error_count=error_count,
                    updated_at=now,
                )
            )
        finally:
            db_connection.close()

    t = threading.Thread(target=_run)
    t.start()
    t.join()


def mark_broadcast_failed_committed(broadcast_id: Any) -> None:
    """Mark a broadcast as FAILED in a separate committed transaction."""
    def _run() -> None:
        from django.db import connection as db_connection
        try:
            (
                GroupBroadcast.objects.unscoped()
                .filter(pk=broadcast_id)
                .update(
                    status=BroadcastStatus.FAILED,
                    updated_at=timezone.now(),
                )
            )
        finally:
            db_connection.close()

    t = threading.Thread(target=_run)
    t.start()
    t.join()


def dispatch_group_broadcast(broadcast: GroupBroadcast) -> dict[str, Any]:
    """Execute a group broadcast campaign across target groups."""
    connection = broadcast.connection
    client = get_evolution_client(connection)

    target_groups = resolve_target_groups(broadcast)
    total_groups = len(target_groups)

    logs: list[dict[str, Any]] = list(broadcast.delivery_logs or [])
    already_sent_jids = {
        entry.get("group_jid") for entry in logs if entry.get("status") == "ok"
    }
    sent_count = len(already_sent_jids)
    error_count = sum(1 for entry in logs if entry.get("status") != "ok")

    broadcast.status = BroadcastStatus.SENDING
    broadcast.total_groups_count = total_groups
    broadcast.sent_count = sent_count
    broadcast.error_count = error_count
    broadcast.delivery_logs = logs
    broadcast.save(update_fields=["status", "total_groups_count", "sent_count", "error_count", "delivery_logs", "updated_at"])

    for idx, group in enumerate(target_groups):
        if group.jid in already_sent_jids:
            logger.info("Group %s already received broadcast %s; skipping", group.jid, broadcast.id)
            continue

        success = False
        error_msg = ""
        try:
            send_message_to_group(broadcast, group, group.jid, client=client)
            success = True
            sent_count += 1
        except Exception as exc:
            error_msg = str(exc)
            error_count += 1
            logger.error("Failed broadcast %s to group %s: %s", broadcast.id, group.jid, exc)

        logs.append({
            "group_jid": group.jid,
            "group_name": group.name,
            "status": "ok" if success else "error",
            "error": error_msg,
            "sent_at": timezone.now().isoformat(),
        })
        broadcast.delivery_logs = logs
        broadcast.sent_count = sent_count
        broadcast.error_count = error_count
        broadcast.updated_at = timezone.now()
        broadcast.save(update_fields=["delivery_logs", "sent_count", "error_count", "updated_at"])
        # Commit checkpoint durably so it survives even if outer caller rolls back
        _save_durable_checkpoint(broadcast.id, logs, sent_count, error_count)

        # Throttle between groups to prevent burst bans
        if idx < total_groups - 1 and broadcast.delay_seconds > 0:
            time.sleep(min(broadcast.delay_seconds, 15))

    broadcast.sent_count = sent_count
    broadcast.error_count = error_count
    broadcast.delivery_logs = logs
    broadcast.sent_at = timezone.now()
    if total_groups > 0 and sent_count == 0:
        broadcast.status = BroadcastStatus.FAILED
    else:
        broadcast.status = BroadcastStatus.COMPLETED
    broadcast.save()

    return {
        "total": total_groups,
        "sent": sent_count,
        "errors": error_count,
    }


def deliver_single_group(broadcast_id: Any, group_jid: str, action: Any = None) -> None:
    """Deliver a group broadcast to one specific WhatsApp group."""
    broadcast = GroupBroadcast.objects.unscoped().filter(pk=broadcast_id).first()
    if not broadcast or broadcast.status == BroadcastStatus.CANCELLED:
        return

    logs = list(broadcast.delivery_logs or [])
    already_sent = {e.get("group_jid") for e in logs if e.get("status") == "ok"}
    if group_jid in already_sent:
        logger.info("Group %s already received broadcast %s; skipping", group_jid, broadcast.id)
        return

    group = WhatsAppGroup.objects.unscoped().filter(connection=broadcast.connection, jid=group_jid).first()
    group_name = group.name if group else group_jid
    success = False
    error_msg = ""
    try:
        send_message_to_group(broadcast, group, group_jid)
        success = True
    except Exception as exc:
        error_msg = str(exc)
        logger.error("Failed broadcast %s to group %s: %s", broadcast.id, group_jid, exc)

    # If delivery failed and queue action has retries remaining, re-raise so worker retries ONLY this group
    if not success and hasattr(action, "attempts") and hasattr(action, "max_attempts") and action.attempts < action.max_attempts:
        raise RuntimeError(f"Delivery to group {group_jid} failed: {error_msg}")

    # Finalize delivery log for this group (durable checkpoint)
    with transaction.atomic():
        broadcast = (
            GroupBroadcast.objects.unscoped()
            .select_for_update()
            .filter(pk=broadcast_id)
            .first()
        )
        if not broadcast:
            return

        logs = list(broadcast.delivery_logs or [])
        logs.append({
            "group_jid": group_jid,
            "group_name": group_name,
            "status": "ok" if success else "error",
            "error": error_msg,
            "sent_at": timezone.now().isoformat(),
        })
        broadcast.delivery_logs = logs
        if success:
            broadcast.sent_count += 1
        else:
            broadcast.error_count += 1

        total = broadcast.total_groups_count
        if total > 0 and len(logs) >= total:
            if broadcast.sent_count > 0:
                broadcast.status = BroadcastStatus.COMPLETED
            else:
                broadcast.status = BroadcastStatus.FAILED
            broadcast.sent_at = timezone.now()

        broadcast.updated_at = timezone.now()
        broadcast.save()


def claim_group_broadcast_for_sending(
    broadcast_id: Any,
    claim_owner: str = "",
    allow_resume: bool = False,
) -> GroupBroadcast | None:
    """Atomically transition a scheduled group broadcast to sending status.

    Guarantees that between concurrent queue workers and hourly housekeeping,
    only one caller can claim and execute the broadcast.
    If allow_resume is True (e.g. queue retry), also allows resuming a broadcast
    currently in SENDING status that is owned by this execution (matching claim_owner).
    Also reclaims stale SENDING broadcasts (> 5 minutes without update).
    """
    with transaction.atomic():
        broadcast = (
            GroupBroadcast.objects.unscoped()
            .select_for_update()
            .filter(pk=broadcast_id)
            .first()
        )
        if not broadcast:
            return None

        now = timezone.now()
        is_stale = (
            broadcast.status == BroadcastStatus.SENDING
            and broadcast.updated_at
            and (now - broadcast.updated_at).total_seconds() > 300
        )

        can_claim = False
        if broadcast.status == BroadcastStatus.SCHEDULED:
            can_claim = True
        elif is_stale:
            can_claim = True
        elif allow_resume and broadcast.status == BroadcastStatus.SENDING:
            if claim_owner and broadcast.claim_token == claim_owner:
                can_claim = True
            elif not broadcast.claim_token and claim_owner:
                can_claim = True

        if not can_claim:
            return None

        broadcast.status = BroadcastStatus.SENDING
        if claim_owner:
            broadcast.claim_token = claim_owner
        broadcast.updated_at = now
        broadcast.save(update_fields=["status", "claim_token", "updated_at"])
        return broadcast


def process_scheduled_broadcasts() -> int:
    """Check for scheduled group broadcasts whose execution time has arrived."""
    pending_ids = list(
        GroupBroadcast.objects.unscoped()
        .filter(
            status=BroadcastStatus.SCHEDULED,
            scheduled_at__lte=timezone.now(),
        )
        .values_list("pk", flat=True)
    )
    processed = 0
    for broadcast_id in pending_ids:
        claim_owner = f"housekeeping:{uuid.uuid4().hex[:12]}"
        broadcast = claim_group_broadcast_for_sending(broadcast_id, claim_owner=claim_owner)
        if not broadcast:
            continue
        try:
            dispatch_group_broadcast(broadcast)
            processed += 1
        except Exception as exc:
            logger.error("Error processing scheduled broadcast %s: %s", broadcast_id, exc)
    return processed
