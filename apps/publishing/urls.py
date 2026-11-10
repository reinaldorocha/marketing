from django.urls import path
from apps.publishing import views

app_name = "publishing"

urlpatterns = [
    path("", views.posts_list, name="list"),
    path("calendar/", views.calendar_view, name="calendar"),
    path("new/", views.post_create, name="create"),
    path("<uuid:post_id>/", views.post_detail, name="detail"),
    path("<uuid:post_id>/edit/", views.post_edit, name="edit"),
    path("<uuid:post_id>/retry/", views.post_retry, name="retry"),
    path("<uuid:post_id>/cancel-schedule/", views.post_cancel_schedule, name="cancel_schedule"),
    path("<uuid:post_id>/refresh/", views.post_refresh, name="refresh"),
    path("<uuid:post_id>/delete/", views.post_delete, name="delete"),
]
