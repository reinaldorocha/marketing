"""Housekeeping jobs for Instagram content publishing."""
from __future__ import annotations

import logging
from django.utils import timezone

from apps.publishing.models import ContentPost, PostStatus
from apps.publishing.services import check_all_pending_posts, publish_post
from apps.queueing.housekeeping import register_housekeeping_job

logger = logging.getLogger(__name__)


@register_housekeeping_job("check_all_scheduled_posts")
def sweep_scheduled_posts() -> str | None:
    """Publish any scheduled posts whose scheduled_at timestamp has arrived."""
    now = timezone.now()
    due_posts = (
        ContentPost.objects.unscoped()
        .filter(status=PostStatus.SCHEDULED, scheduled_at__lte=now)
        .select_related("connection")
        .order_by("scheduled_at")[:20]
    )

    published = 0
    errors = 0
    for post in due_posts:
        try:
            publish_post(post)
            published += 1
        except Exception as exc:
            logger.exception("Error executing scheduled post %s: %s", post.pk, exc)
            errors += 1

    if published > 0 or errors > 0:
        return f"scheduled posts: {published} published, {errors} errors"
    return None


@register_housekeeping_job("check_all_pending_posts")
def sweep_pending_posts() -> str | None:
    """Poll and finalize any posts waiting for Meta video/carousel transcoding."""
    count = check_all_pending_posts()
    if count > 0:
        return f"finalized {count} pending Instagram posts"
    return None
