import logging
import time
from typing import Any
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
    conn_phone = (connection.external_id or "").split("@")[0].replace("+", "").strip()

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
                if conn_phone in p_jid:
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


def dispatch_group_broadcast(broadcast: GroupBroadcast) -> dict[str, Any]:
    """Execute a group broadcast campaign across target groups."""
    connection = broadcast.connection
    client = get_evolution_client(connection)

    # Resolve target groups
    target_groups_qs = WhatsAppGroup.objects.none()
    if broadcast.target_mode == BroadcastTargetMode.SELECTED:
        target_groups_qs = broadcast.target_groups.all()
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

    target_groups = list(target_groups_qs)
    total_groups = len(target_groups)

    broadcast.status = BroadcastStatus.SENDING
    broadcast.total_groups_count = total_groups
    broadcast.sent_count = 0
    broadcast.error_count = 0
    broadcast.delivery_logs = []
    broadcast.save()

    logs: list[dict[str, Any]] = []
    sent_count = 0
    error_count = 0

    for idx, group in enumerate(target_groups):
        mentions: list[str] = []
        if broadcast.mention_all and group.participants_cache:
            for p in group.participants_cache:
                p_jid = ""
                if isinstance(p, dict):
                    p_jid = str(p.get("JID") or p.get("jid") or "")
                elif isinstance(p, str):
                    p_jid = p
                if p_jid:
                    mentions.append(p_jid)

        success = False
        error_msg = ""
        try:
            mtype = broadcast.message_type
            if mtype == BroadcastMessageType.TEXT:
                client.send_text(
                    number=group.jid,
                    text=broadcast.text,
                    mentions=mentions if broadcast.mention_all else None,
                )
            elif mtype == BroadcastMessageType.MEDIA:
                client.send_media(
                    number=group.jid,
                    media_type=broadcast.media_type or "image",
                    url=broadcast.media_url,
                    caption=broadcast.media_caption,
                    mentions=mentions if broadcast.mention_all else None,
                )
            elif mtype == BroadcastMessageType.POLL:
                client.send_poll(
                    number=group.jid,
                    question=broadcast.poll_question,
                    options=broadcast.poll_options or ["Sim", "Não"],
                    max_answers=broadcast.poll_max_answers or 1,
                )
            elif mtype == BroadcastMessageType.STICKER:
                client.send_sticker(
                    number=group.jid,
                    url=broadcast.sticker_url,
                )
            elif mtype == BroadcastMessageType.LOCATION:
                client.send_location(
                    number=group.jid,
                    latitude=broadcast.location_latitude or 0.0,
                    longitude=broadcast.location_longitude or 0.0,
                    name=broadcast.location_name,
                    address=broadcast.location_address,
                )
            elif mtype == BroadcastMessageType.CONTACT:
                client.send_contact(
                    number=group.jid,
                    full_name=broadcast.contact_name,
                    phone=broadcast.contact_phone,
                    organization=broadcast.contact_organization,
                )
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


def process_scheduled_broadcasts() -> int:
    """Check for scheduled group broadcasts whose execution time has arrived."""
    pending = GroupBroadcast.objects.unscoped().filter(
        status=BroadcastStatus.SCHEDULED,
        scheduled_at__lte=timezone.now(),
    )
    processed = 0
    for broadcast in pending:
        try:
            dispatch_group_broadcast(broadcast)
            processed += 1
        except Exception as exc:
            logger.error("Error processing scheduled broadcast %s: %s", broadcast.id, exc)
    return processed
