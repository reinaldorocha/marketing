"""Housekeeping jobs for Instagram content publishing."""
from __future__ import annotations

import logging
import uuid
from django.db import transaction
from django.utils import timezone

from apps.publishing.models import ContentPost, PostStatus
from apps.publishing.services import check_all_pending_posts, publish_post, recover_abandoned_claims
from apps.queueing.housekeeping import register_housekeeping_job

logger = logging.getLogger(__name__)


@register_housekeeping_job("check_all_scheduled_posts")
def sweep_scheduled_posts() -> str | None:
    """Publish any scheduled posts whose scheduled_at timestamp has arrived."""
    now = timezone.now()
    due_posts: list[tuple[ContentPost, str]] = []

    with transaction.atomic():
        candidates = list(
            ContentPost.objects.unscoped()
            .select_for_update(skip_locked=True)
            .filter(status=PostStatus.SCHEDULED, scheduled_at__lte=now)
            .select_related("connection")
            .order_by("scheduled_at")[:20]
        )
        for post in candidates:
            execution_id = uuid.uuid4().hex[:16]
            claim_token = f"claim:{execution_id}:{int(timezone.now().timestamp())}"
            post.status = PostStatus.PUBLISHING
            post.container_id = claim_token
            post.save(update_fields=["status", "container_id", "updated_at"])
            due_posts.append((post, claim_token))

    published = 0
    errors = 0
    for post, execution_id in due_posts:
        try:
            publish_post(post, execution_id=execution_id)
            published += 1
        except Exception as exc:
            logger.exception("Error executing scheduled post %s: %s", post.pk, exc)
            errors += 1

    if published > 0 or errors > 0:
        return f"scheduled posts: {published} published, {errors} errors"
    return None


@register_housekeeping_job("check_all_pending_posts")
def sweep_pending_posts() -> str | None:
    """Poll and finalize any posts waiting for Meta video/carousel transcoding, and recover abandoned claims."""
    recovered = recover_abandoned_claims()
    count = check_all_pending_posts()
    parts = []
    if recovered > 0:
        parts.append(f"recovered {recovered} abandoned publishing claims")
    if count > 0:
        parts.append(f"finalized {count} pending Instagram posts")
    return ", ".join(parts) if parts else None
