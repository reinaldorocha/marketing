from django.apps import AppConfig


class PublishingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.publishing"
    verbose_name = "Publicações e Calendário Editorial"

    def ready(self) -> None:
        from apps.publishing import handlers  # noqa: F401
        from apps.publishing import housekeeping  # noqa: F401
