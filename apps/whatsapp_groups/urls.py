from django.urls import path
from apps.whatsapp_groups import views

app_name = "whatsapp_groups"

urlpatterns = [
    # Groups
    path("", views.groups_list, name="list"),
    path("sync/", views.group_sync, name="sync"),
    path("create/", views.group_create, name="create"),
    path("<uuid:group_id>/", views.group_detail, name="detail"),
    path("<uuid:group_id>/edit/", views.group_edit, name="edit"),
    path("<uuid:group_id>/invite/", views.group_refresh_invite, name="refresh_invite"),
    path("<uuid:group_id>/leave/", views.group_leave, name="leave"),

    # Broadcasts
    path("broadcasts/", views.broadcast_list, name="broadcast_list"),
    path("broadcasts/compose/", views.broadcast_compose, name="broadcast_compose"),
    path("broadcasts/<uuid:broadcast_id>/", views.broadcast_detail, name="broadcast_detail"),
    path("broadcasts/<uuid:broadcast_id>/cancel/", views.broadcast_cancel, name="broadcast_cancel"),
    path("broadcasts/<uuid:broadcast_id>/reconcile-group/", views.broadcast_reconcile_group, name="broadcast_reconcile_group"),

    # Tags
    path("tags/", views.tags_manage, name="tags_manage"),
]
