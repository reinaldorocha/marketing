import uuid
from django.db import models
from django.utils import timezone

from apps.channels.models import ChannelConnection
from apps.common.scoping import WorkspaceScopedModel


class PublishType(models.TextChoices):
    FEED_IMAGE = "feed_image", "Feed Imagem"
    FEED_VIDEO = "feed_video", "Feed Vídeo"
    REEL_VIDEO = "reel_video", "Reel Vídeo"
    STORY_IMAGE = "story_image", "Story Imagem"
    STORY_VIDEO = "story_video", "Story Vídeo"
    CAROUSEL = "carousel", "Carrossel"


class PostStatus(models.TextChoices):
    DRAFT = "draft", "Rascunho"
    PUBLISHING = "publishing", "Processando na Meta"
    PUBLISHED = "published", "Publicado"
    FAILED = "failed", "Falha"


class ContentPost(WorkspaceScopedModel):
    """An Instagram post (Feed, Reel, Story or Carousel) managed through BrightBean."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    connection = models.ForeignKey(
        ChannelConnection,
        on_delete=models.CASCADE,
        related_name="content_posts",
        help_text="The Instagram channel connection used for publishing.",
    )
    publish_type = models.CharField(
        max_length=32,
        choices=PublishType.choices,
        default=PublishType.FEED_IMAGE,
    )
    caption = models.TextField(blank=True, default="")
    media_url = models.URLField(
        max_length=2000,
        blank=True,
        default="",
        help_text="Public URL for single image/video.",
    )
    cover_url = models.URLField(
        max_length=2000,
        blank=True,
        null=True,
        help_text="Optional cover URL for reels or video.",
    )
    media_items = models.JSONField(
        default=list,
        blank=True,
        help_text="List of items for carousel: [{'type': 'image'|'video', 'url': '...', 'cover_url': '...'}]",
    )
    container_id = models.CharField(max_length=128, blank=True, default="")
    published_media_id = models.CharField(max_length=128, blank=True, default="")
    permalink = models.URLField(max_length=2000, blank=True, default="")
    status = models.CharField(
        max_length=32,
        choices=PostStatus.choices,
        default=PostStatus.DRAFT,
        db_index=True,
    )
    last_error = models.TextField(blank=True, default="")
    scheduled_at = models.DateTimeField(null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "publishing_content_post"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"ContentPost({self.publish_type}: {self.status} - {self.caption[:30]})"
