"""Queue action handlers for asynchronous and scheduled Instagram publishing."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Any

from django.db import transaction
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

    execution_id = uuid.uuid4().hex[:16]
    claim_token = f"claim:{execution_id}:{int(timezone.now().timestamp())}"

    with transaction.atomic():
        post = (
            ContentPost.objects.select_for_update(skip_locked=True)
            .for_workspace(action.workspace_id)
            .select_related("connection")
            .filter(pk=post_id)
            .first()
        )
        if not post:
            logger.info("publish_content_post: post %s locked by another process or not found; skipping", post_id)
            return

        # Check status: must be currently SCHEDULED
        if post.status != PostStatus.SCHEDULED:
            logger.info("publish_content_post: post %s is in status %s, not SCHEDULED; skipping", post.pk, post.status)
            return

        # Check if rescheduled to a future time (strict: no 60s window!)
        if post.scheduled_at and post.scheduled_at > timezone.now():
            logger.info(
                "publish_content_post: post %s scheduled_at %s is still in the future (now=%s); skipping",
                post.pk,
                post.scheduled_at,
                timezone.now(),
            )
            return

        # Check if this task matches the post's current scheduled_at (normalize across timezones)
        task_scheduled_at = payload.get("scheduled_at")
        if task_scheduled_at and post.scheduled_at:
            try:
                task_dt = datetime.fromisoformat(task_scheduled_at)
                if timezone.is_naive(task_dt):
                    task_dt = timezone.make_aware(task_dt, dt_timezone.utc)
                else:
                    task_dt = task_dt.astimezone(dt_timezone.utc)

                post_dt = post.scheduled_at
                if timezone.is_naive(post_dt):
                    post_dt = timezone.make_aware(post_dt, dt_timezone.utc)
                else:
                    post_dt = post_dt.astimezone(dt_timezone.utc)

                if abs((post_dt - task_dt).total_seconds()) > 1.0:
                    logger.info(
                        "publish_content_post: post %s was rescheduled from %s to %s; skipping stale task",
                        post.pk,
                        task_dt.isoformat(),
                        post_dt.isoformat(),
                    )
                    return
            except (ValueError, TypeError):
                pass

        post.status = PostStatus.PUBLISHING
        post.container_id = claim_token
        post.save(update_fields=["status", "container_id", "updated_at"])

    logger.info("publish_content_post: dispatching post %s to Meta API (claim=%s)", post.pk, claim_token)
    publish_post(post, execution_id=claim_token)

    # If post is in progress (video/carousel transcoding on Meta), schedule automatic background polling
    if post.status == PostStatus.PUBLISHING and post.container_id and not post.container_id.startswith("claim:"):
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

    if not post_id or not container_id:
        return

    with transaction.atomic():
        post = (
            ContentPost.objects.select_for_update(skip_locked=True)
            .for_workspace(action.workspace_id)
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

    if not finished and post.status == PostStatus.PUBLISHING and post.container_id == container_id and check_count < 10:
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
