"""The public API, mounted at ``/api/v1/`` by ``config/urls.py``."""

from django.urls import path

from apps.api import views_docs, views_inbound_webhooks
from apps.api.api import api

urlpatterns = [
    path("docs", views_docs.api_docs, name="api_docs"),
    path("leads/webhook/<uuid:webhook_id>/", views_inbound_webhooks.inbound_lead_receiver, name="inbound_lead_receiver"),
    path("", api.urls),
]
