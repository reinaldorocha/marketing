from django.apps import AppConfig


class WhatsAppGroupsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.whatsapp_groups"
    verbose_name = "WhatsApp Groups"

    def ready(self) -> None:
        from apps.whatsapp_groups import handlers, housekeeping  # noqa: F401
