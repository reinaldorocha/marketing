import uuid
from django.db import models
from django.utils import timezone

from apps.channels.models import ChannelConnection
from apps.common.scoping import WorkspaceScopedModel


class WhatsAppGroupTag(WorkspaceScopedModel):
    """Tag / Label applied to WhatsApp groups for segmentation and targeted broadcasting."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=50)
    color = models.CharField(max_length=20, default="#25D366")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Etiqueta de Grupo"
        verbose_name_plural = "Etiquetas de Grupos"
        unique_together = ("workspace", "name")
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class WhatsAppGroup(WorkspaceScopedModel):
    """A WhatsApp group synchronized from an Evolution GO instance."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    connection = models.ForeignKey(
        ChannelConnection,
        on_delete=models.CASCADE,
        related_name="whatsapp_groups",
        help_text="The Evolution GO WhatsApp connection this group belongs to.",
    )
    jid = models.CharField(
        max_length=128,
        db_index=True,
        help_text="WhatsApp group JID (e.g. 120363024849201948@g.us).",
    )
    name = models.CharField(max_length=255, default="", blank=True)
    description = models.TextField(blank=True, default="")
    owner_jid = models.CharField(max_length=128, blank=True, default="")
    invite_code = models.CharField(max_length=255, blank=True, default="")
    is_announce = models.BooleanField(
        default=False,
        help_text="True if only group admins are permitted to send messages.",
    )
    is_admin = models.BooleanField(
        default=False,
        help_text="True if our connected instance has admin privileges in this group.",
    )
    participants_count = models.PositiveIntegerField(default=0)
    participants_cache = models.JSONField(
        default=list,
        blank=True,
        help_text="Cached list of group participant JIDs or dictionaries for @everyone mentions.",
    )
    tags = models.ManyToManyField(
        WhatsAppGroupTag,
        blank=True,
        related_name="groups",
        help_text="Labels applied to this group for segmentation.",
    )
    last_synced_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Grupo WhatsApp"
        verbose_name_plural = "Grupos WhatsApp"
        unique_together = ("connection", "jid")
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name or self.jid

    @property
    def invite_link(self) -> str:
        if not self.invite_code:
            return ""
        if self.invite_code.startswith("http"):
            return self.invite_code
        return f"https://chat.whatsapp.com/{self.invite_code}"


class BroadcastTargetMode(models.TextChoices):
    SELECTED = "selected", "Grupos Selecionados"
    BY_TAG = "by_tag", "Por Etiquetas de Grupos"
    ALL = "all", "Todos os Grupos Conectados"


class BroadcastMessageType(models.TextChoices):
    TEXT = "text", "Texto"
    MEDIA = "media", "Mídia (Imagem, Vídeo, Documento, Áudio)"
    POLL = "poll", "Enquete Interativa"
    STICKER = "sticker", "Figurinha / Sticker"
    LOCATION = "location", "Localização GPS"
    CONTACT = "contact", "Contato (vCard)"


class BroadcastStatus(models.TextChoices):
    DRAFT = "draft", "Rascunho"
    SCHEDULED = "scheduled", "Agendado"
    SENDING = "sending", "Disparando"
    COMPLETED = "completed", "Concluído"
    FAILED = "failed", "Falha"
    CANCELLED = "cancelled", "Cancelado"


class GroupBroadcast(WorkspaceScopedModel):
    """A broadcast campaign sent to one or multiple WhatsApp groups."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    connection = models.ForeignKey(
        ChannelConnection,
        on_delete=models.CASCADE,
        related_name="group_broadcasts",
        help_text="Evolution GO connection used for this broadcast.",
    )
    title = models.CharField(max_length=255, help_text="Nome de identificação do disparo.")
    target_mode = models.CharField(
        max_length=20,
        choices=BroadcastTargetMode.choices,
        default=BroadcastTargetMode.SELECTED,
    )
    target_groups = models.ManyToManyField(
        WhatsAppGroup,
        blank=True,
        related_name="broadcasts",
    )
    target_tags = models.ManyToManyField(
        WhatsAppGroupTag,
        blank=True,
        related_name="broadcasts",
    )
    message_type = models.CharField(
        max_length=32,
        choices=BroadcastMessageType.choices,
        default=BroadcastMessageType.TEXT,
    )

    # Content fields
    text = models.TextField(blank=True, default="")
    media_url = models.URLField(max_length=2000, blank=True, default="")
    media_type = models.CharField(max_length=20, blank=True, default="image")
    media_caption = models.TextField(blank=True, default="")

    # Special formats: Poll
    poll_question = models.CharField(max_length=255, blank=True, default="")
    poll_options = models.JSONField(default=list, blank=True)
    poll_max_answers = models.PositiveIntegerField(default=1)

    # Special formats: Sticker
    sticker_url = models.URLField(max_length=2000, blank=True, default="")

    # Special formats: Location
    location_latitude = models.FloatField(null=True, blank=True)
    location_longitude = models.FloatField(null=True, blank=True)
    location_name = models.CharField(max_length=255, blank=True, default="")
    location_address = models.CharField(max_length=255, blank=True, default="")

    # Special formats: Contact vCard
    contact_name = models.CharField(max_length=255, blank=True, default="")
    contact_phone = models.CharField(max_length=50, blank=True, default="")
    contact_organization = models.CharField(max_length=255, blank=True, default="")

    # Group options
    mention_all = models.BooleanField(
        default=False,
        help_text="Marcar todos os participantes do grupo (@everyone).",
    )
    delay_seconds = models.PositiveIntegerField(
        default=3,
        help_text="Intervalo em segundos entre envios em cada grupo para prevenir bloqueio.",
    )

    # Execution & Status
    status = models.CharField(
        max_length=20,
        choices=BroadcastStatus.choices,
        default=BroadcastStatus.DRAFT,
    )
    scheduled_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    # Metrics and logs
    total_groups_count = models.PositiveIntegerField(default=0)
    sent_count = models.PositiveIntegerField(default=0)
    error_count = models.PositiveIntegerField(default=0)
    delivery_logs = models.JSONField(
        default=list,
        blank=True,
        help_text="Log entries: [{'group_jid': '...', 'group_name': '...', 'status': 'ok'|'error', 'error': '...', 'sent_at': '...'}]",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Disparo em Grupos"
        verbose_name_plural = "Disparos em Grupos"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.title} ({self.get_status_display()})"
