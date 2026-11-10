import logging
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Any
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.channels.models import ChannelConnection, ConnectionStatus
from apps.channels.providers.whatsapp_evolution import EvolutionClient, WhatsAppEvolutionAdapter
from apps.common.platforms import Platform
from apps.queueing.models import ActionStatus, ScheduledAction
from apps.queueing.registry import schedule
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


def _run_in_independent_thread(func: Any) -> Any:
    """Execute func in an independent thread with its own database connection.

    Ensures that:
    1. The operation commits immediately in its own transaction, avoiding long-lived row locks.
    2. Any exception raised inside func (such as OperationalError, DatabaseError, etc.)
       is captured and re-raised in the caller's thread upon t.join(), so queue workers
       can properly catch and retry the action.
    """
    result: list[Any] = []
    error: list[BaseException] = []

    def _worker() -> None:
        from django.db import connection as db_connection
        try:
            res = func()
            result.append(res)
        except BaseException as exc:
            error.append(exc)
        finally:
            db_connection.close()

    t = threading.Thread(target=_worker)
    t.start()
    t.join()

    if error:
        raise error[0]
    return result[0] if result else None


def mark_broadcast_failed_committed(broadcast_id: Any) -> None:
    """Mark a broadcast as FAILED in an independent committed transaction.

    Used when fanout or execution fails terminally, ensuring that the terminal
    FAILED status is committed independently and survives worker rollback.
    """
    def _fail() -> None:
        (
            GroupBroadcast.objects.unscoped()
            .filter(pk=broadcast_id)
            .update(
                status=BroadcastStatus.FAILED,
                updated_at=timezone.now(),
            )
        )

    _run_in_independent_thread(_fail)


def set_broadcast_total_groups_committed(broadcast_id: Any, total_groups: int) -> None:
    """Update total_groups_count in an independent committed transaction."""
    def _update() -> None:
        (
            GroupBroadcast.objects.unscoped()
            .filter(pk=broadcast_id)
            .update(
                total_groups_count=total_groups,
                updated_at=timezone.now(),
            )
        )

    _run_in_independent_thread(_update)


def set_broadcast_completed_committed(broadcast_id: Any, total_groups: int | None = None) -> None:
    """Set broadcast status to COMPLETED in an independent committed transaction."""
    def _complete() -> None:
        now = timezone.now()
        update_fields: dict[str, Any] = {
            "status": BroadcastStatus.COMPLETED,
            "sent_at": now,
            "updated_at": now,
        }
        if total_groups is not None:
            update_fields["total_groups_count"] = total_groups
        (
            GroupBroadcast.objects.unscoped()
            .filter(pk=broadcast_id)
            .update(**update_fields)
        )

    _run_in_independent_thread(_complete)


def _release_claim_on_failure(broadcast_id: Any, claim_owner: str) -> None:
    """Reset claim_token and revert status to SCHEDULED only if owned by claim_owner."""
    if not claim_owner:
        return

    def _release() -> None:
        with transaction.atomic():
            b = (
                GroupBroadcast.objects.unscoped()
                .select_for_update()
                .filter(pk=broadcast_id, claim_token=claim_owner)
                .first()
            )
            if not b:
                return

            update_kwargs: dict[str, Any] = {"claim_token": "", "updated_at": timezone.now()}
            has_successful_deliveries = any(
                entry.get("status") == "ok" for entry in (b.delivery_logs or [])
            )
            if not has_successful_deliveries:
                update_kwargs["status"] = BroadcastStatus.SCHEDULED

            (
                GroupBroadcast.objects.unscoped()
                .filter(pk=broadcast_id, claim_token=claim_owner)
                .update(**update_kwargs)
            )

    try:
        _run_in_independent_thread(_release)
    except Exception as exc:
        logger.warning("Failed to release claim %s for broadcast %s: %s", claim_owner, broadcast_id, exc)


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


def reserve_group_delivery(
    broadcast_id: Any,
    group_jid: str,
    action_id: str = "",
) -> tuple[bool, str]:
    """Atomically reserve a specific WhatsApp group for delivery in an independent transaction.

    Returns (True, "reserved") if the reservation was granted.
    Returns (False, "already_sent") if the group was already successfully sent.
    Returns (False, "in_flight") if another worker is actively sending to this group.
    Returns (False, "not_found") if the broadcast does not exist or is cancelled.
    """
    def _reserve() -> tuple[bool, str]:
        with transaction.atomic():
            broadcast = (
                GroupBroadcast.objects.unscoped()
                .select_for_update()
                .filter(pk=broadcast_id)
                .first()
            )
            if not broadcast or broadcast.status == BroadcastStatus.CANCELLED:
                return False, "not_found"

            logs = list(broadcast.delivery_logs or [])
            if any(e.get("group_jid") == group_jid and e.get("status") == "ok" for e in logs):
                return False, "already_sent"

            now = timezone.now()
            # Check for active in_flight reservation (< 300 seconds)
            active_in_flight = False
            for e in logs:
                if e.get("group_jid") == group_jid and e.get("status") == "in_flight":
                    reserved_by = e.get("action_id", "")
                    # If this reservation belongs to the same action that is retrying, allow reclaiming!
                    if action_id and reserved_by and action_id == reserved_by:
                        active_in_flight = False
                        break

                    sent_at_str = e.get("sent_at")
                    if sent_at_str:
                        try:
                            sent_time = datetime.fromisoformat(sent_at_str)
                            if (now - sent_time).total_seconds() < 300:
                                active_in_flight = True
                                break
                        except Exception:
                            pass
                    else:
                        active_in_flight = True
                        break

            if active_in_flight:
                return False, "in_flight"

            # Filter out any old in_flight entry for this group and reserve anew
            new_logs = [
                e for e in logs
                if not (e.get("group_jid") == group_jid and e.get("status") == "in_flight")
            ]
            new_logs.append({
                "group_jid": group_jid,
                "status": "in_flight",
                "action_id": action_id,
                "sent_at": now.isoformat(),
            })
            broadcast.delivery_logs = new_logs
            broadcast.updated_at = now
            broadcast.save(update_fields=["delivery_logs", "updated_at"])
            return True, "reserved"

    return _run_in_independent_thread(_reserve)


def finalize_group_delivery(
    broadcast_id: Any,
    group_jid: str,
    group_name: str,
    success: bool,
    error_msg: str,
    will_retry: bool,
) -> None:
    """Commit the delivery result for a specific WhatsApp group in an independent transaction."""
    def _finalize() -> None:
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
            # Remove in_flight reservation
            logs = [
                e for e in logs
                if not (e.get("group_jid") == group_jid and e.get("status") == "in_flight")
            ]

            now = timezone.now()
            if success:
                # Add terminal success log if not already present
                if not any(e.get("group_jid") == group_jid and e.get("status") == "ok" for e in logs):
                    logs.append({
                        "group_jid": group_jid,
                        "group_name": group_name,
                        "status": "ok",
                        "error": "",
                        "sent_at": now.isoformat(),
                    })
                    broadcast.sent_count += 1
            else:
                if not will_retry:
                    # Final attempt failed: record terminal error log
                    logs.append({
                        "group_jid": group_jid,
                        "group_name": group_name,
                        "status": "error",
                        "error": error_msg,
                        "sent_at": now.isoformat(),
                    })
                    broadcast.error_count += 1

            broadcast.delivery_logs = logs

            # If all target groups have reached a terminal result (ok or error), mark broadcast complete
            terminal_count = sum(1 for e in logs if e.get("status") in ("ok", "error"))
            total = broadcast.total_groups_count
            if total > 0 and terminal_count >= total:
                if broadcast.sent_count > 0:
                    broadcast.status = BroadcastStatus.COMPLETED
                else:
                    broadcast.status = BroadcastStatus.FAILED
                broadcast.sent_at = now

            broadcast.updated_at = now
            broadcast.save()

    _run_in_independent_thread(_finalize)


def deliver_single_group(broadcast_id: Any, group_jid: str, action: Any = None) -> None:
    """Deliver a group broadcast to one specific WhatsApp group with atomic reservation."""
    action_id = str(action.pk) if hasattr(action, "pk") and action.pk else ""
    reserved, reason = reserve_group_delivery(broadcast_id, group_jid, action_id=action_id)
    if not reserved:
        if reason in ("already_sent", "not_found"):
            logger.info(
                "Group %s for broadcast %s cannot be reserved (reason: %s); completing normally",
                group_jid,
                broadcast_id,
                reason,
            )
            return

        if reason == "in_flight":
            # Another worker or attempt is actively delivering to this group right now.
            # We must NOT conclude this task as DONE. Raise to trigger queue retry/backoff.
            raise RuntimeError(
                f"Group {group_jid} delivery for broadcast {broadcast_id} is currently in-flight; rescheduling"
            )
        return

    group_name = group_jid
    success = False
    error_msg = ""
    try:
        broadcast = GroupBroadcast.objects.unscoped().filter(pk=broadcast_id).first()
        if not broadcast or broadcast.status == BroadcastStatus.CANCELLED:
            # Broadcast was cancelled while reserving: clean up in_flight reservation
            finalize_group_delivery(
                broadcast_id=broadcast_id,
                group_jid=group_jid,
                group_name=group_name,
                success=False,
                error_msg="Cancelled",
                will_retry=False,
            )
            return

        group = WhatsAppGroup.objects.unscoped().filter(connection=broadcast.connection, jid=group_jid).first()
        if group and group.name:
            group_name = group.name

        send_message_to_group(broadcast, group, group_jid)
        success = True
    except Exception as exc:
        error_msg = str(exc)
        logger.error("Failed broadcast %s to group %s: %s", broadcast_id, group_jid, exc)
    finally:
        will_retry = (
            not success
            and hasattr(action, "attempts")
            and hasattr(action, "max_attempts")
            and action.attempts < action.max_attempts
        )

        finalize_group_delivery(
            broadcast_id=broadcast_id,
            group_jid=group_jid,
            group_name=group_name,
            success=success,
            error_msg=error_msg,
            will_retry=will_retry,
        )

    if not success:
        raise RuntimeError(f"Delivery to group {group_jid} failed: {error_msg}")


def claim_group_broadcast_for_sending(
    broadcast_id: Any,
    claim_owner: str = "",
    allow_resume: bool = False,
) -> GroupBroadcast | None:
    """Atomically transition a scheduled group broadcast to sending status in an independent transaction.

    Guarantees that between concurrent queue workers and hourly housekeeping,
    only one caller can claim and execute the broadcast.
    If allow_resume is True (e.g. queue retry), also allows resuming a broadcast
    currently in SENDING status that is owned by this execution (matching claim_owner).
    Also reclaims stale SENDING broadcasts (> 5 minutes without update).

    Crucially, committing the claim in an independent transaction releases the row
    lock immediately, so the caller's transaction never maintains an exclusive lock
    that could cause circular wait with other operations.
    Propagates database exceptions (e.g. OperationalError) to trigger queue retries.
    """
    def _claim() -> GroupBroadcast | None:
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

    return _run_in_independent_thread(_claim)


def process_scheduled_broadcasts() -> int:
    """Check for scheduled and abandoned group broadcasts whose execution time has arrived.

    Sweeps for:
    1. SCHEDULED broadcasts due for sending (scheduled_at <= now)
    2. Stale / abandoned SENDING broadcasts (> 5 minutes without update)

    For each candidate, reclaims the broadcast, ensures total_groups_count is set,
    and forwards remaining unsent groups to per-group queue tasks.
    Checks existing ScheduledAction rows to avoid duplicate tasks or double-schedules.
    """
    now = timezone.now()
    stale_cutoff = now - timedelta(seconds=300)

    pending_ids = list(
        GroupBroadcast.objects.unscoped()
        .filter(
            Q(status=BroadcastStatus.SCHEDULED, scheduled_at__lte=now)
            | Q(status=BroadcastStatus.SENDING, updated_at__lte=stale_cutoff)
        )
        .values_list("pk", flat=True)
    )
    processed = 0
    for broadcast_id in pending_ids:
        claim_owner = f"housekeeping:{uuid.uuid4().hex[:12]}"
        broadcast = None
        try:
            broadcast = claim_group_broadcast_for_sending(
                broadcast_id, claim_owner=claim_owner, allow_resume=True
            )
            if not broadcast:
                continue

            target_groups = resolve_target_groups(broadcast)
            total_groups = len(target_groups)
            logs = list(broadcast.delivery_logs or [])
            already_sent_jids = {
                entry.get("group_jid") for entry in logs if entry.get("status") == "ok"
            }
            terminal_error_jids = {
                entry.get("group_jid") for entry in logs if entry.get("status") == "error"
            }
            terminal_jids = already_sent_jids | terminal_error_jids

            # Check for actively in-flight groups (< 5 min)
            active_in_flight_jids = set()
            for entry in logs:
                if entry.get("status") == "in_flight":
                    sent_at_str = entry.get("sent_at")
                    if sent_at_str:
                        try:
                            st = datetime.fromisoformat(sent_at_str)
                            if (now - st).total_seconds() < 300:
                                active_in_flight_jids.add(entry.get("group_jid"))
                        except Exception:
                            pass

            remaining_groups = [
                g for g in target_groups
                if g.jid not in already_sent_jids and g.jid not in active_in_flight_jids
            ]

            if total_groups == 0:
                set_broadcast_completed_committed(broadcast.id, total_groups=0)
                processed += 1
                continue

            if len(terminal_jids) >= total_groups:
                if len(already_sent_jids) > 0:
                    set_broadcast_completed_committed(broadcast.id, total_groups=total_groups)
                else:
                    mark_broadcast_failed_committed(broadcast.id)
                processed += 1
                continue

            set_broadcast_total_groups_committed(broadcast.id, total_groups)

            delay = min(max(broadcast.delay_seconds, 0), 15)
            for idx, group in enumerate(remaining_groups):
                action_key = f"group_broadcast:{broadcast.id}:{group.jid}"
                existing_action = (
                    ScheduledAction.objects.unscoped()
                    .filter(type="group_broadcast_send", idempotency_key=action_key)
                    .first()
                )

                if existing_action:
                    if existing_action.status in (ActionStatus.PENDING, ActionStatus.RUNNING):
                        # Task is already pending or actively executing: do not duplicate
                        continue
                    elif existing_action.status == ActionStatus.DONE:
                        # Reconcile: If the task is marked DONE but the group has no terminal
                        # delivery result in delivery_logs (neither 'ok' nor 'error'), it completed
                        # prematurely or was dropped. Re-arm it to guarantee delivery!
                        if group.jid in terminal_jids:
                            continue
                        logger.warning(
                            "Reconciling group %s for broadcast %s: task is DONE without terminal delivery result",
                            group.jid,
                            broadcast.id,
                        )
                        existing_action.status = ActionStatus.PENDING
                        existing_action.attempts = 0
                        existing_action.run_at = now + timedelta(seconds=idx * delay)
                        existing_action.last_error = ""
                        existing_action.save(
                            update_fields=["status", "attempts", "run_at", "last_error", "updated_at"]
                        )
                    elif existing_action.status == ActionStatus.FAILED:
                        # Task exhausted retries and group has no terminal delivery: re-arm it for recovery
                        if group.jid in terminal_jids:
                            continue
                        existing_action.status = ActionStatus.PENDING
                        existing_action.attempts = 0
                        existing_action.run_at = now + timedelta(seconds=idx * delay)
                        existing_action.last_error = ""
                        existing_action.save(
                            update_fields=["status", "attempts", "run_at", "last_error", "updated_at"]
                        )
                else:
                    schedule(
                        "group_broadcast_send",
                        run_at=now + timedelta(seconds=idx * delay),
                        payload={"broadcast_id": str(broadcast.id), "group_jid": group.jid},
                        workspace=broadcast.workspace,
                        idempotency_key=action_key,
                    )
            processed += 1
        except Exception as exc:
            logger.error("Error processing scheduled broadcast %s: %s", broadcast_id, exc)
            if broadcast is not None:
                _release_claim_on_failure(broadcast_id, claim_owner=claim_owner)
    return processed
