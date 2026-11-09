"""Queue action handlers for asynchronous and scheduled Instagram publishing."""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from django.utils import timezone

from apps.publishing.models import ContentPost, PostStatus
from apps.publishing.services import publish_post, refresh_post_status
from apps.queueing.models import ScheduledAction
from apps.queueing.registry import register_handler, schedule

logger = logging.getLogger(__name__)


@register_handler("publish_content_post")
def handle_publish_content_post(payload: dict[str, Any], action: ScheduledAction) -> None:
    """Execute scheduled publication of an Instagram ContentPost."""
    post_id = payload.get("post_id")
    if not post_id:
        logger.warning("publish_content_post: missing post_id in payload: %s", payload)
        return

    post = (
        ContentPost.objects.for_workspace(action.workspace_id)
        .select_related("connection")
        .filter(pk=post_id)
        .first()
    )
    if not post:
        logger.warning("publish_content_post: ContentPost %s not found in workspace %s", post_id, action.workspace_id)
        return

    # Check status to prevent duplicate publishing
    if post.status in (PostStatus.PUBLISHED, PostStatus.PUBLISHING):
        logger.info("publish_content_post: post %s already published or in progress; skipping", post.pk)
        return

    logger.info("publish_content_post: dispatching post %s to Meta API", post.pk)
    publish_post(post)

    # If post is in progress (video/carousel transcoding on Meta), schedule automatic background polling
    if post.status == PostStatus.PUBLISHING and post.container_id:
        next_poll = timezone.now() + timedelta(seconds=30)
        try:
            schedule(
                "poll_publishing_container",
                next_poll,
                {"post_id": str(post.pk), "container_id": post.container_id, "check_count": 1},
                workspace=post.workspace,
                idempotency_key=f"publishing:poll:{post.pk}:{post.container_id}:1",
            )
            logger.info("publish_content_post: enqueued poll_publishing_container for post %s at %s", post.pk, next_poll)
        except Exception as exc:
            logger.warning("Could not schedule poll_publishing_container: %s", exc)


@register_handler("poll_publishing_container")
def handle_poll_publishing_container(payload: dict[str, Any], action: ScheduledAction) -> None:
    """Poll Meta Graph API for container status and finalize when finished."""
    post_id = payload.get("post_id")
    container_id = payload.get("container_id", "")
    check_count = int(payload.get("check_count", 1))

    if not post_id:
        return

    post = (
        ContentPost.objects.for_workspace(action.workspace_id)
        .select_related("connection")
        .filter(pk=post_id)
        .first()
    )
    if not post:
        return

    # If container changed or post is no longer PUBLISHING, stop polling
    if post.status != PostStatus.PUBLISHING or post.container_id != container_id:
        return

    finished = refresh_post_status(post)
    if not finished and post.status == PostStatus.PUBLISHING and check_count < 10:
        # Schedule next check in 30 seconds
        next_poll = timezone.now() + timedelta(seconds=30)
        try:
            schedule(
                "poll_publishing_container",
                next_poll,
                {"post_id": str(post.pk), "container_id": post.container_id, "check_count": check_count + 1},
                workspace=post.workspace,
                idempotency_key=f"publishing:poll:{post.pk}:{post.container_id}:{check_count + 1}",
            )
        except Exception as exc:
            logger.warning("Could not reschedule poll_publishing_container: %s", exc)
