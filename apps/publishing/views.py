import json
from typing import Any
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from apps.channels.models import ChannelConnection, ConnectionStatus
from apps.common.platforms import Platform
from apps.members.decorators import require_permission
from apps.members.requests import WorkspaceRequest
from apps.publishing.models import ContentPost, PostStatus, PublishType
from apps.publishing.services import publish_post, refresh_post_status


@login_required
@require_permission("manage_channels")
@require_GET
def posts_list(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """List published and draft posts with filter controls and stats."""
    status_filter = request.GET.get("status", "all")
    type_filter = request.GET.get("type", "all")
    q = request.GET.get("q", "").strip()

    qs = ContentPost.objects.for_workspace(request.workspace).select_related("connection")

    total_count = qs.count()
    published_count = qs.filter(status=PostStatus.PUBLISHED).count()
    publishing_count = qs.filter(status=PostStatus.PUBLISHING).count()
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
            "publishing": publishing_count,
            "failed": failed_count,
        },
    }
    return render(request, "publishing/list.html", context)


@login_required
@require_permission("manage_channels")
@require_http_methods(["GET", "POST"])
def post_create(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """Create and immediately publish or draft an Instagram post."""
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
        else:
            messages.success(request, "Rascunho de post salvo com sucesso.")
            return redirect("publishing:list", workspace_id=workspace_id)

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
    }
    return render(request, "publishing/create.html", context)


@login_required
@require_permission("manage_channels")
@require_GET
def post_detail(request: WorkspaceRequest, workspace_id: str, post_id: str) -> HttpResponse:
    """View details of a post, status on Meta, permalink, and errors."""
    post = get_object_or_404(ContentPost.objects.for_workspace(request.workspace), pk=post_id)

    context = {
        "post": post,
    }
    return render(request, "publishing/detail.html", context)


@login_required
@require_permission("manage_channels")
@require_POST
def post_refresh(request: WorkspaceRequest, workspace_id: str, post_id: str) -> HttpResponse:
    """HTMX endpoint to refresh container processing status from Meta."""
    post = get_object_or_404(ContentPost.objects.for_workspace(request.workspace), pk=post_id)
    refresh_post_status(post)

    context = {
        "post": post,
    }
    return render(request, "publishing/_status_badge.html", context)


@login_required
@require_permission("manage_channels")
@require_POST
def post_delete(request: WorkspaceRequest, workspace_id: str, post_id: str) -> HttpResponse:
    """Delete a post record."""
    post = get_object_or_404(ContentPost.objects.for_workspace(request.workspace), pk=post_id)
    post.delete()
    messages.success(request, "Publicação removida do histórico.")
    return redirect("publishing:list", workspace_id=workspace_id)
