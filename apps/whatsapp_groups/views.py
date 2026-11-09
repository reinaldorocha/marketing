import json
from typing import Any
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from apps.channels.models import ChannelConnection, ConnectionStatus
from apps.common.platforms import Platform
from apps.common.shortcuts import get_scoped_object_or_404
from apps.members.decorators import require_permission
from apps.members.requests import WorkspaceRequest
from apps.whatsapp_groups.models import (
    BroadcastMessageType,
    BroadcastStatus,
    BroadcastTargetMode,
    GroupBroadcast,
    WhatsAppGroup,
    WhatsAppGroupTag,
)
from apps.whatsapp_groups.services import (
    create_group_on_evolution,
    dispatch_group_broadcast,
    leave_group_on_evolution,
    refresh_group_invite_link,
    sync_groups_from_evolution,
    update_group_on_evolution,
)


@login_required
@require_permission("send_broadcasts")
@require_GET
def groups_list(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """List all synchronized WhatsApp groups with filter and search capabilities."""
    workspace = request.workspace
    q = request.GET.get("q", "").strip()
    conn_id = request.GET.get("connection", "").strip()
    tag_id = request.GET.get("tag", "").strip()

    connections = ChannelConnection.objects.for_workspace(workspace).filter(
        platform=Platform.WHATSAPP_EVOLUTION.value,
        status=ConnectionStatus.ACTIVE,
    )

    groups_qs = WhatsAppGroup.objects.for_workspace(workspace).select_related("connection").prefetch_related("tags")

    if conn_id:
        groups_qs = groups_qs.filter(connection_id=conn_id)
    if tag_id:
        groups_qs = groups_qs.filter(tags__id=tag_id)
    if q:
        groups_qs = groups_qs.filter(name__icontains=q)

    total_groups = groups_qs.count()
    admin_groups = groups_qs.filter(is_admin=True).count()
    all_tags = WhatsAppGroupTag.objects.for_workspace(workspace)

    context = {
        "groups": groups_qs[:100],
        "total_groups": total_groups,
        "admin_groups": admin_groups,
        "connections": connections,
        "tags": all_tags,
        "selected_conn_id": conn_id,
        "selected_tag_id": tag_id,
        "q": q,
        "active_tab": "groups",
    }
    return render(request, "whatsapp_groups/list.html", context)


@login_required
@require_permission("manage_channels")
@require_POST
def group_sync(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """Trigger group synchronization from Evolution GO."""
    workspace = request.workspace
    conn_id = request.POST.get("connection_id")

    connections = ChannelConnection.objects.for_workspace(workspace).filter(
        platform=Platform.WHATSAPP_EVOLUTION.value,
        status=ConnectionStatus.ACTIVE,
    )

    if conn_id:
        connections = connections.filter(pk=conn_id)

    total_synced = 0
    for conn in connections:
        try:
            count = sync_groups_from_evolution(conn)
            total_synced += count
        except Exception as exc:
            messages.error(request, f"Erro ao sincronizar conexão {conn.name}: {exc}")

    if total_synced > 0:
        messages.success(request, f"{total_synced} grupo(s) sincronizado(s) com sucesso!")
    else:
        messages.info(request, "Sincronização concluída. Nenhum novo grupo encontrado.")

    return redirect("whatsapp_groups:list", workspace_id=workspace_id)


@login_required
@require_permission("manage_channels")
@require_http_methods(["GET", "POST"])
def group_create(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """Create a new group directly on WhatsApp via Evolution GO."""
    workspace = request.workspace
    connections = ChannelConnection.objects.for_workspace(workspace).filter(
        platform=Platform.WHATSAPP_EVOLUTION.value,
        status=ConnectionStatus.ACTIVE,
    )

    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        conn_id = request.POST.get("connection_id", "").strip()
        raw_participants = request.POST.get("participants", "").strip()

        if not name:
            messages.error(request, "O nome do grupo é obrigatório.")
            return redirect("whatsapp_groups:create", workspace_id=workspace_id)

        connection = connections.filter(pk=conn_id).first()
        if not connection:
            messages.error(request, "Selecione uma conexão Evolution GO ativa.")
            return redirect("whatsapp_groups:create", workspace_id=workspace_id)

        # Parse participants (one per line or comma-separated)
        participants = []
        for line in raw_participants.replace(",", "\n").splitlines():
            line = line.strip()
            if line:
                participants.append(line)

        try:
            group = create_group_on_evolution(connection, name, participants)
            messages.success(request, f"Grupo '{group.name}' criado com sucesso no WhatsApp!")
            return redirect("whatsapp_groups:detail", workspace_id=workspace_id, group_id=group.id)
        except Exception as exc:
            messages.error(request, f"Erro ao criar grupo: {exc}")

    return render(
        request,
        "whatsapp_groups/group_create.html",
        {"connections": connections, "active_tab": "groups"},
    )


@login_required
@require_permission("send_broadcasts")
@require_GET
def group_detail(request: WorkspaceRequest, workspace_id: str, group_id: str) -> HttpResponse:
    """View detailed information and participants of a specific group."""
    workspace = request.workspace
    group = get_scoped_object_or_404(WhatsAppGroup, workspace, pk=group_id)
    all_tags = WhatsAppGroupTag.objects.for_workspace(workspace)
    recent_broadcasts = group.broadcasts.order_by("-created_at")[:10]

    return render(
        request,
        "whatsapp_groups/detail.html",
        {
            "group": group,
            "all_tags": all_tags,
            "recent_broadcasts": recent_broadcasts,
            "active_tab": "groups",
        },
    )


@login_required
@require_permission("manage_channels")
@require_POST
def group_edit(request: WorkspaceRequest, workspace_id: str, group_id: str) -> HttpResponse:
    """Edit group subject, description, announcement mode, and labels/tags."""
    workspace = request.workspace
    group = get_scoped_object_or_404(WhatsAppGroup, workspace, pk=group_id)

    name = request.POST.get("name", "").strip()
    description = request.POST.get("description", "").strip()
    is_announce = request.POST.get("is_announce") == "on"
    tag_ids = request.POST.getlist("tags")

    try:
        update_group_on_evolution(
            group,
            subject=name if name else None,
            description=description,
            announce=is_announce,
        )
        if tag_ids is not None:
            group.tags.set(tag_ids)
        messages.success(request, "Configurações do grupo atualizadas com sucesso!")
    except Exception as exc:
        messages.error(request, f"Erro ao atualizar grupo: {exc}")

    return redirect("whatsapp_groups:detail", workspace_id=workspace_id, group_id=group.id)


@login_required
@require_permission("manage_channels")
@require_POST
def group_refresh_invite(request: WorkspaceRequest, workspace_id: str, group_id: str) -> HttpResponse:
    """Refresh or revoke group invite code."""
    workspace = request.workspace
    group = get_scoped_object_or_404(WhatsAppGroup, workspace, pk=group_id)
    revoke = request.POST.get("revoke") == "1"

    try:
        link = refresh_group_invite_link(group, revoke=revoke)
        if link:
            messages.success(request, f"Link de convite gerado: {link}")
        else:
            messages.warning(request, "Não foi possível obter o link de convite. Verifique se o bot é admin.")
    except Exception as exc:
        messages.error(request, f"Erro ao gerar link: {exc}")

    return redirect("whatsapp_groups:detail", workspace_id=workspace_id, group_id=group.id)


@login_required
@require_permission("manage_channels")
@require_POST
def group_leave(request: WorkspaceRequest, workspace_id: str, group_id: str) -> HttpResponse:
    """Leave group on WhatsApp and delete from local database."""
    workspace = request.workspace
    group = get_scoped_object_or_404(WhatsAppGroup, workspace, pk=group_id)
    name = group.name

    try:
        leave_group_on_evolution(group)
        messages.success(request, f"Você saiu do grupo '{name}' com sucesso.")
    except Exception as exc:
        messages.error(request, f"Erro ao sair do grupo: {exc}")

    return redirect("whatsapp_groups:list", workspace_id=workspace_id)


# ---------------------------------------------------------------------------
# Broadcasts em Grupos (Disparos, Agendamentos, Menção a Todos)
# ---------------------------------------------------------------------------


@login_required
@require_permission("send_broadcasts")
@require_GET
def broadcast_list(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """List all group broadcast campaigns with stats and status filters."""
    workspace = request.workspace
    status_filter = request.GET.get("status", "all")

    broadcasts_qs = GroupBroadcast.objects.for_workspace(workspace).select_related("connection")
    if status_filter != "all":
        broadcasts_qs = broadcasts_qs.filter(status=status_filter)

    total_count = GroupBroadcast.objects.for_workspace(workspace).count()
    completed_count = GroupBroadcast.objects.for_workspace(workspace).filter(status=BroadcastStatus.COMPLETED).count()
    scheduled_count = GroupBroadcast.objects.for_workspace(workspace).filter(status=BroadcastStatus.SCHEDULED).count()
    failed_count = GroupBroadcast.objects.for_workspace(workspace).filter(status=BroadcastStatus.FAILED).count()

    context = {
        "broadcasts": broadcasts_qs[:50],
        "status_filter": status_filter,
        "total_count": total_count,
        "completed_count": completed_count,
        "scheduled_count": scheduled_count,
        "failed_count": failed_count,
        "active_tab": "broadcasts",
    }
    return render(request, "whatsapp_groups/broadcast_list.html", context)


@login_required
@require_permission("send_broadcasts")
@require_http_methods(["GET", "POST"])
def broadcast_compose(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """Compose and send or schedule a message broadcast to WhatsApp groups."""
    workspace = request.workspace
    connections = ChannelConnection.objects.for_workspace(workspace).filter(
        platform=Platform.WHATSAPP_EVOLUTION.value,
        status=ConnectionStatus.ACTIVE,
    )
    groups = WhatsAppGroup.objects.for_workspace(workspace).order_by("name")
    tags = WhatsAppGroupTag.objects.for_workspace(workspace)

    if request.method == "POST":
        title = request.POST.get("title", "").strip() or f"Disparo {timezone.now().strftime('%d/%m/%Y %H:%M')}"
        conn_id = request.POST.get("connection_id", "").strip()
        target_mode = request.POST.get("target_mode", BroadcastTargetMode.SELECTED)
        message_type = request.POST.get("message_type", BroadcastMessageType.TEXT)
        mention_all = request.POST.get("mention_all") == "on"
        delay_seconds = int(request.POST.get("delay_seconds", 3) or 3)
        schedule_time_raw = request.POST.get("scheduled_at", "").strip()

        connection = connections.filter(pk=conn_id).first()
        if not connection:
            messages.error(request, "Selecione uma conexão Evolution GO ativa.")
            return redirect("whatsapp_groups:broadcast_compose", workspace_id=workspace_id)

        # Build broadcast instance
        broadcast = GroupBroadcast.objects.for_workspace(workspace).create(
            connection=connection,
            title=title,
            target_mode=target_mode,
            message_type=message_type,
            mention_all=mention_all,
            delay_seconds=delay_seconds,
            text=request.POST.get("text", "").strip(),
            media_type=request.POST.get("media_type", "image"),
            media_url=request.POST.get("media_url", "").strip(),
            media_caption=request.POST.get("media_caption", "").strip(),
            poll_question=request.POST.get("poll_question", "").strip(),
            poll_options=[opt.strip() for opt in request.POST.get("poll_options", "").splitlines() if opt.strip()],
            poll_max_answers=int(request.POST.get("poll_max_answers", 1) or 1),
            sticker_url=request.POST.get("sticker_url", "").strip(),
            location_latitude=float(request.POST.get("location_latitude")) if request.POST.get("location_latitude") else None,
            location_longitude=float(request.POST.get("location_longitude")) if request.POST.get("location_longitude") else None,
            location_name=request.POST.get("location_name", "").strip(),
            location_address=request.POST.get("location_address", "").strip(),
            contact_name=request.POST.get("contact_name", "").strip(),
            contact_phone=request.POST.get("contact_phone", "").strip(),
            contact_organization=request.POST.get("contact_organization", "").strip(),
        )

        # Attach target groups or tags
        if target_mode == BroadcastTargetMode.SELECTED:
            group_ids = request.POST.getlist("target_groups")
            broadcast.target_groups.set(group_ids)
        elif target_mode == BroadcastTargetMode.BY_TAG:
            tag_ids = request.POST.getlist("target_tags")
            broadcast.target_tags.set(tag_ids)

        # Handle scheduling vs immediate sending
        if schedule_time_raw:
            scheduled_dt = parse_datetime(schedule_time_raw)
            if scheduled_dt:
                if timezone.is_naive(scheduled_dt):
                    scheduled_dt = timezone.make_aware(scheduled_dt)
                broadcast.scheduled_at = scheduled_dt
                broadcast.status = BroadcastStatus.SCHEDULED
                broadcast.save(update_fields=["scheduled_at", "status"])
                messages.success(request, f"Disparo agendado com sucesso para {scheduled_dt.strftime('%d/%m/%Y às %H:%M')}!")
                return redirect("whatsapp_groups:broadcast_detail", workspace_id=workspace_id, broadcast_id=broadcast.id)

        # Send immediately
        try:
            res = dispatch_group_broadcast(broadcast)
            messages.success(
                request,
                f"Disparo concluído: {res['sent']} grupo(s) com sucesso, {res['errors']} com erro.",
            )
        except Exception as exc:
            messages.error(request, f"Erro ao processar disparo: {exc}")

        return redirect("whatsapp_groups:broadcast_detail", workspace_id=workspace_id, broadcast_id=broadcast.id)

    return render(
        request,
        "whatsapp_groups/broadcast_compose.html",
        {
            "connections": connections,
            "groups": groups,
            "tags": tags,
            "active_tab": "broadcasts",
        },
    )


@login_required
@require_permission("send_broadcasts")
@require_GET
def broadcast_detail(request: WorkspaceRequest, workspace_id: str, broadcast_id: str) -> HttpResponse:
    """Show detailed delivery results and logs for a group broadcast."""
    workspace = request.workspace
    broadcast = get_scoped_object_or_404(GroupBroadcast, workspace, pk=broadcast_id)

    return render(
        request,
        "whatsapp_groups/broadcast_detail.html",
        {
            "broadcast": broadcast,
            "logs": broadcast.delivery_logs,
            "active_tab": "broadcasts",
        },
    )


@login_required
@require_permission("send_broadcasts")
@require_POST
def broadcast_cancel(request: WorkspaceRequest, workspace_id: str, broadcast_id: str) -> HttpResponse:
    """Cancel a scheduled group broadcast."""
    workspace = request.workspace
    broadcast = get_scoped_object_or_404(GroupBroadcast, workspace, pk=broadcast_id)

    if broadcast.status == BroadcastStatus.SCHEDULED:
        broadcast.status = BroadcastStatus.CANCELLED
        broadcast.save(update_fields=["status", "updated_at"])
        messages.success(request, "Disparo agendado cancelado com sucesso.")
    else:
        messages.warning(request, "Apenas disparos agendados podem ser cancelados.")

    return redirect("whatsapp_groups:broadcast_detail", workspace_id=workspace_id, broadcast_id=broadcast.id)


# ---------------------------------------------------------------------------
# Gestão de Etiquetas / Tags de Grupos
# ---------------------------------------------------------------------------


@login_required
@require_permission("send_broadcasts")
@require_http_methods(["GET", "POST"])
def tags_manage(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """Manage WhatsApp group tags for segmentation."""
    workspace = request.workspace

    if request.method == "POST":
        action = request.POST.get("action", "create")
        if action == "create":
            name = request.POST.get("name", "").strip()
            color = request.POST.get("color", "#25D366").strip()
            if name:
                WhatsAppGroupTag.objects.for_workspace(workspace).get_or_create(
                    name=name,
                    defaults={"color": color},
                )
                messages.success(request, f"Etiqueta '{name}' criada!")
        elif action == "delete":
            tag_id = request.POST.get("tag_id")
            WhatsAppGroupTag.objects.for_workspace(workspace).filter(pk=tag_id).delete()
            messages.success(request, "Etiqueta removida com sucesso.")

        return redirect("whatsapp_groups:tags_manage", workspace_id=workspace_id)

    tags = WhatsAppGroupTag.objects.for_workspace(workspace)
    return render(
        request,
        "whatsapp_groups/tags_manage.html",
        {"tags": tags, "active_tab": "tags"},
    )
