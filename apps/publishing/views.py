"""Views for Instagram publishing: editorial calendar, scheduling, creation, and detail."""
from __future__ import annotations

import calendar
import datetime
import json
import logging
from typing import Any

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.channels.models import ChannelConnection, ConnectionStatus
from apps.common.platforms import Platform
from apps.members.decorators import require_permission
from apps.members.requests import WorkspaceRequest
from apps.publishing.models import ContentPost, PostStatus, PublishType
from apps.publishing.services import (
    cancel_schedule,
    publish_post,
    refresh_post_status,
    retry_post,
    schedule_post,
)

logger = logging.getLogger(__name__)

MONTH_NAMES_PT = [
    "Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
    "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro"
]


@login_required
@require_permission("manage_channels")
@require_GET
def posts_list(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """List published, scheduled, and draft posts with filter controls and stats."""
    status_filter = request.GET.get("status", "all")
    type_filter = request.GET.get("type", "all")
    q = request.GET.get("q", "").strip()

    qs = ContentPost.objects.for_workspace(request.workspace).select_related("connection")

    total_count = qs.count()
    published_count = qs.filter(status=PostStatus.PUBLISHED).count()
    scheduled_count = qs.filter(status=PostStatus.SCHEDULED).count()
    publishing_count = qs.filter(status=PostStatus.PUBLISHING).count()
    draft_count = qs.filter(status=PostStatus.DRAFT).count()
    failed_count = qs.filter(status=PostStatus.FAILED).count()

    if status_filter != "all":
        qs = qs.filter(status=status_filter)
    if type_filter != "all":
        qs = qs.filter(publish_type=type_filter)
    if q:
        qs = qs.filter(caption__icontains=q)

    # Check for active Instagram connection
    has_instagram = ChannelConnection.objects.for_workspace(request.workspace).filter(
        platform=Platform.INSTAGRAM.value, status=ConnectionStatus.ACTIVE
    ).exists()

    context = {
        "posts": qs[:50],
        "status_filter": status_filter,
        "type_filter": type_filter,
        "q": q,
        "has_instagram": has_instagram,
        "stats": {
            "total": total_count,
            "published": published_count,
            "scheduled": scheduled_count,
            "publishing": publishing_count,
            "draft": draft_count,
            "failed": failed_count,
        },
    }
    return render(request, "publishing/list.html", context)


@login_required
@require_permission("manage_channels")
@require_GET
def calendar_view(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """Full monthly editorial calendar showing scheduled, published, and draft posts."""
    now = timezone.now()

    # Parse requested year and month or default to current
    try:
        year = int(request.GET.get("year", now.year))
        month = int(request.GET.get("month", now.month))
        if not (1 <= month <= 12):
            month = now.month
    except (ValueError, TypeError):
        year = now.year
        month = now.month

    # Prev / Next month navigation
    if month == 1:
        prev_month, prev_year = 12, year - 1
    else:
        prev_month, prev_year = month - 1, year

    if month == 12:
        next_month, next_year = 1, year + 1
    else:
        next_month, next_year = month + 1, year

    # Month date boundaries
    first_day = datetime.date(year, month, 1)
    last_day_num = calendar.monthrange(year, month)[1]
    last_day = datetime.date(year, month, last_day_num)

    # Query all posts relevant to this month (scheduled_at, published_at, or created_at)
    month_start = timezone.make_aware(datetime.datetime.combine(first_day - datetime.timedelta(days=7), datetime.time.min))
    month_end = timezone.make_aware(datetime.datetime.combine(last_day + datetime.timedelta(days=7), datetime.time.max))

    posts_qs = (
        ContentPost.objects.for_workspace(request.workspace)
        .select_related("connection")
        .filter(
            (models_Q(scheduled_at__gte=month_start, scheduled_at__lte=month_end))
            | (models_Q(published_at__gte=month_start, published_at__lte=month_end))
            | (models_Q(status=PostStatus.DRAFT, created_at__gte=month_start, created_at__lte=month_end))
        )
    )

    connection_filter = request.GET.get("connection", "all")
    if connection_filter != "all":
        posts_qs = posts_qs.filter(connection_id=connection_filter)

    # Group posts by day string "YYYY-MM-DD"
    posts_by_day: dict[str, list[ContentPost]] = {}
    for post in posts_qs:
        # Determine relevant date for the calendar
        target_date: datetime.date | None = None
        if post.scheduled_at:
            target_date = timezone.localtime(post.scheduled_at).date()
        elif post.published_at:
            target_date = timezone.localtime(post.published_at).date()
        else:
            target_date = timezone.localtime(post.created_at).date()

        date_key = target_date.strftime("%Y-%m-%d")
        if date_key not in posts_by_day:
            posts_by_day[date_key] = []
        posts_by_day[date_key].append(post)

    # Build calendar matrix using Sunday as first day of week
    cal = calendar.Calendar(firstweekday=6)
    month_days = cal.monthdatescalendar(year, month)

    calendar_weeks = []
    today_date = timezone.localtime(now).date()

    for week in month_days:
        week_days = []
        for day in week:
            date_key = day.strftime("%Y-%m-%d")
            day_posts = posts_by_day.get(date_key, [])
            week_days.append({
                "date": day,
                "date_str": date_key,
                "day_number": day.day,
                "is_current_month": day.month == month,
                "is_today": day == today_date,
                "posts": day_posts,
                "posts_count": len(day_posts),
            })
        calendar_weeks.append(week_days)

    connections = ChannelConnection.objects.for_workspace(request.workspace).filter(
        platform=Platform.INSTAGRAM.value, status=ConnectionStatus.ACTIVE
    )

    context = {
        "year": year,
        "month": month,
        "month_name": MONTH_NAMES_PT[month - 1],
        "prev_year": prev_year,
        "prev_month": prev_month,
        "next_year": next_year,
        "next_month": next_month,
        "calendar_weeks": calendar_weeks,
        "connections": connections,
        "connection_filter": connection_filter,
        "today_str": today_date.strftime("%Y-%m-%d"),
    }
    return render(request, "publishing/calendar.html", context)


from django.db.models import Q as models_Q


@login_required
@require_permission("manage_channels")
@require_http_methods(["GET", "POST"])
def post_create(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """Create and immediately publish, schedule, or draft an Instagram post."""
    connections = ChannelConnection.objects.for_workspace(request.workspace).filter(
        platform=Platform.INSTAGRAM.value, status=ConnectionStatus.ACTIVE
    )

    if not connections.exists():
        messages.warning(request, "Conecte uma conta profissional do Instagram antes de criar uma publicação.")
        return redirect("channels:instagram_connect", workspace_id=workspace_id)

    if request.method == "POST":
        connection_id = request.POST.get("connection_id")
        connection = connections.filter(pk=connection_id).first() or connections.first()
        publish_type = request.POST.get("publish_type", PublishType.FEED_IMAGE)
        caption = request.POST.get("caption", "").strip()
        media_url = request.POST.get("media_url", "").strip()
        cover_url = request.POST.get("cover_url", "").strip() or None
        action = request.POST.get("action", "publish")
        scheduled_at_raw = request.POST.get("scheduled_at", "").strip()

        # Parse carousel items if carousel
        media_items = []
        if publish_type == PublishType.CAROUSEL:
            types = request.POST.getlist("carousel_type")
            urls = request.POST.getlist("carousel_url")
            covers = request.POST.getlist("carousel_cover")
            for t, u, c in zip(types, urls, covers):
                u = u.strip()
                if u:
                    media_items.append({"type": t, "url": u, "cover_url": c.strip() or None})

        post = ContentPost.objects.create(
            workspace=request.workspace,
            connection=connection,
            publish_type=publish_type,
            caption=caption,
            media_url=media_url,
            cover_url=cover_url,
            media_items=media_items,
            status=PostStatus.DRAFT,
        )

        if action == "publish":
            publish_post(post)
            if post.status == PostStatus.PUBLISHED:
                messages.success(request, "Publicação realizada no Instagram com sucesso!")
            elif post.status == PostStatus.PUBLISHING:
                messages.info(request, "Mídia em processamento na Meta. O status será atualizado em instantes.")
            else:
                messages.error(request, f"Falha na publicação: {post.last_error}")
            return redirect("publishing:detail", workspace_id=workspace_id, post_id=post.pk)

        elif action == "schedule" and scheduled_at_raw:
            try:
                # Expecting format 'YYYY-MM-DDTHH:MM' from datetime-local
                scheduled_dt = datetime.datetime.fromisoformat(scheduled_at_raw)
                if timezone.is_naive(scheduled_dt):
                    scheduled_dt = timezone.make_aware(scheduled_dt)
                schedule_post(post, scheduled_dt)
                messages.success(request, f"Publicação agendada para {scheduled_dt.strftime('%d/%m/%Y às %H:%M')}.")
                return redirect("publishing:calendar", workspace_id=workspace_id)
            except Exception as exc:
                messages.warning(request, f"Data de agendamento inválida ({exc}). Salvo como rascunho.")
                return redirect("publishing:detail", workspace_id=workspace_id, post_id=post.pk)

        else:
            messages.success(request, "Rascunho de post salvo com sucesso.")
            return redirect("publishing:list", workspace_id=workspace_id)

    # Check for pre-selected date from calendar
    preset_date = request.GET.get("date", "")
    preset_datetime = f"{preset_date}T10:00" if preset_date else ""

    # Try to load recent media from media_library for convenience
    recent_assets = []
    try:
        from apps.media_library.models import MediaAsset
        recent_assets = MediaAsset.objects.for_workspace(request.workspace).order_by("-created_at")[:12]
    except Exception:
        pass

    context = {
        "connections": connections,
        "publish_types": PublishType.choices,
        "recent_assets": recent_assets,
        "preset_datetime": preset_datetime,
    }
    return render(request, "publishing/create.html", context)


@login_required
@require_permission("manage_channels")
@require_http_methods(["GET", "POST"])
def post_edit(request: WorkspaceRequest, workspace_id: str, post_id: str) -> HttpResponse:
    """Edit an existing draft, scheduled, or failed post."""
    post = get_object_or_404(ContentPost.objects.for_workspace(request.workspace), pk=post_id)

    if post.status == PostStatus.PUBLISHED:
        messages.warning(request, "Posts já publicados não podem ser editados.")
        return redirect("publishing:detail", workspace_id=workspace_id, post_id=post.pk)

    connections = ChannelConnection.objects.for_workspace(request.workspace).filter(
        platform=Platform.INSTAGRAM.value, status=ConnectionStatus.ACTIVE
    )

    if request.method == "POST":
        target_post = None
        with transaction.atomic():
            locked = (
                ContentPost.objects.for_workspace(request.workspace)
                .select_for_update()
                .filter(pk=post.pk)
                .first()
            )
            if not locked:
                messages.error(request, "Publicação não encontrada.")
                return redirect("publishing:calendar", workspace_id=workspace_id)

            if locked.status == PostStatus.PUBLISHED or locked.published_media_id:
                messages.warning(request, "Esta publicação já foi concluída por outro processo e não pode ser editada.")
                return redirect("publishing:detail", workspace_id=workspace_id, post_id=post.pk)

            if locked.status == PostStatus.PUBLISHING:
                messages.warning(request, "Esta publicação está sendo processada no momento e não pode ser editada.")
                return redirect("publishing:detail", workspace_id=workspace_id, post_id=post.pk)

            connection_id = request.POST.get("connection_id")
            connection = connections.filter(pk=connection_id).first() or locked.connection
            publish_type = request.POST.get("publish_type", locked.publish_type)
            caption = request.POST.get("caption", "").strip()
            media_url = request.POST.get("media_url", "").strip()
            cover_url = request.POST.get("cover_url", "").strip() or None
            action = request.POST.get("action", "save")
            scheduled_at_raw = request.POST.get("scheduled_at", "").strip()

            # Parse carousel items without dropping existing items if not provided
            media_items = locked.media_items if isinstance(locked.media_items, list) else []
            if publish_type == PublishType.CAROUSEL:
                types = request.POST.getlist("carousel_type")
                urls = request.POST.getlist("carousel_url")
                covers = request.POST.getlist("carousel_cover")
                parsed = []
                for t, u, c in zip(types, urls, covers):
                    u = u.strip()
                    if u:
                        parsed.append({"type": t, "url": u, "cover_url": c.strip() or None})
                if parsed or "carousel_type" in request.POST:
                    media_items = parsed

            locked.connection = connection
            locked.publish_type = publish_type
            locked.caption = caption
            locked.media_url = media_url
            locked.cover_url = cover_url
            locked.media_items = media_items
            locked.save(update_fields=["connection", "publish_type", "caption", "media_url", "cover_url", "media_items", "updated_at"])

            if action == "publish":
                target_post = locked
            elif action == "schedule" and scheduled_at_raw:
                try:
                    scheduled_dt = datetime.datetime.fromisoformat(scheduled_at_raw)
                    if timezone.is_naive(scheduled_dt):
                        scheduled_dt = timezone.make_aware(scheduled_dt)
                    schedule_post(locked, scheduled_dt)
                    messages.success(request, f"Publicação reagendada para {scheduled_dt.strftime('%d/%m/%Y às %H:%M')}.")
                    return redirect("publishing:calendar", workspace_id=workspace_id)
                except Exception as exc:
                    messages.warning(request, f"Data inválida ({exc}). Alterações salvas como rascunho.")
                    return redirect("publishing:detail", workspace_id=workspace_id, post_id=post.pk)
            else:
                # Action "save" (Salvar Rascunho)
                if locked.status == PostStatus.SCHEDULED:
                    cancel_schedule(locked)
                    messages.success(request, "Agendamento cancelado e publicação salva como rascunho.")
                else:
                    locked.status = PostStatus.DRAFT
                    locked.save(update_fields=["status", "updated_at"])
                    messages.success(request, "Rascunho salvo com sucesso.")
                return redirect("publishing:detail", workspace_id=workspace_id, post_id=post.pk)

        if action == "publish" and target_post:
            publish_post(target_post)
            messages.success(request, "Publicação enviada para a Meta com sucesso.")
            return redirect("publishing:detail", workspace_id=workspace_id, post_id=post.pk)

    formatted_scheduled_at = ""
    if post.scheduled_at:
        formatted_scheduled_at = timezone.localtime(post.scheduled_at).strftime("%Y-%m-%dT%H:%M")

    context = {
        "post": post,
        "connections": connections,
        "publish_types": PublishType.choices,
        "formatted_scheduled_at": formatted_scheduled_at,
    }
    return render(request, "publishing/edit.html", context)


@login_required
@require_permission("manage_channels")
@require_POST
def post_retry(request: WorkspaceRequest, workspace_id: str, post_id: str) -> HttpResponse:
    """One-click retry for a failed post or stuck publishing claim."""
    post = get_object_or_404(ContentPost.objects.for_workspace(request.workspace), pk=post_id)
    prev_status = post.status
    retry_post(post)
    if post.status == PostStatus.PUBLISHED:
        messages.success(request, "Post publicado no Instagram com sucesso!")
    elif post.status == PostStatus.PUBLISHING:
        if prev_status == PostStatus.PUBLISHING and not post.last_error:
            messages.info(request, "Post em processamento na Meta.")
        else:
            messages.info(request, "Nova tentativa iniciada: post em processamento na Meta.")
    elif post.status == PostStatus.FAILED:
        messages.error(request, f"Falha na nova tentativa: {post.last_error}")
    else:
        messages.warning(request, f"O post está com status '{post.get_status_display()}' e não pode ser reenviado agora.")

    return redirect("publishing:detail", workspace_id=workspace_id, post_id=post.pk)


@login_required
@require_permission("manage_channels")
@require_POST
def post_cancel_schedule(request: WorkspaceRequest, workspace_id: str, post_id: str) -> HttpResponse:
    """Cancel scheduling of a post and return it to draft."""
    post = get_object_or_404(ContentPost.objects.for_workspace(request.workspace), pk=post_id)
    cancel_schedule(post)
    messages.info(request, "Agendamento cancelado. O post retornou para o status de Rascunho.")
    return redirect("publishing:detail", workspace_id=workspace_id, post_id=post.pk)


@login_required
@require_permission("manage_channels")
@require_GET
def post_detail(request: WorkspaceRequest, workspace_id: str, post_id: str) -> HttpResponse:
    """View details of a post, status on Meta, permalink, scheduling, and errors."""
    post = get_object_or_404(ContentPost.objects.for_workspace(request.workspace), pk=post_id)
    return render(request, "publishing/detail.html", {"post": post})


@login_required
@require_permission("manage_channels")
@require_POST
def post_refresh(request: WorkspaceRequest, workspace_id: str, post_id: str) -> HttpResponse:
    """HTMX endpoint to refresh container processing status from Meta."""
    post = get_object_or_404(ContentPost.objects.for_workspace(request.workspace), pk=post_id)
    refresh_post_status(post)
    return render(request, "publishing/_status_badge.html", {"post": post})


@login_required
@require_permission("manage_channels")
@require_POST
def post_delete(request: WorkspaceRequest, workspace_id: str, post_id: str) -> HttpResponse:
    """Delete a post record."""
    post = get_object_or_404(ContentPost.objects.for_workspace(request.workspace), pk=post_id)
    post.delete()
    messages.success(request, "Publicação removida com sucesso.")
    return redirect("publishing:list", workspace_id=workspace_id)
