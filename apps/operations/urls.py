"""URL configuration for Central Operacional."""
from django.urls import path
from apps.operations import views

app_name = "operations"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("actions/<uuid:action_id>/retry/", views.action_retry, name="action_retry"),
    path("actions/<uuid:action_id>/cancel/", views.action_cancel, name="action_cancel"),
    path("actions/<uuid:action_id>/detail/", views.action_detail, name="action_detail"),
    path("bulk-retry/", views.bulk_retry, name="bulk_retry"),
    path("channels/<uuid:connection_id>/test/", views.channel_test, name="channel_test"),
]
