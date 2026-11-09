"""Lead webhooks workspace routes, mounted at /w/<uuid:workspace_id>/settings/lead-webhooks/."""
from django.urls import path

from apps.api import views_inbound_webhooks

app_name = "lead_webhooks"

urlpatterns = [
    path("", views_inbound_webhooks.lead_webhook_list, name="list"),
    path("create/", views_inbound_webhooks.lead_webhook_create, name="create"),
    path("<uuid:webhook_id>/", views_inbound_webhooks.lead_webhook_detail, name="detail"),
    path("<uuid:webhook_id>/rotate-secret/", views_inbound_webhooks.lead_webhook_rotate_secret, name="rotate_secret"),
    path("<uuid:webhook_id>/delete/", views_inbound_webhooks.lead_webhook_delete, name="delete"),
]
