from django.urls import path
from apps.publishing import views

app_name = "publishing"

urlpatterns = [
    path("", views.posts_list, name="list"),
    path("new/", views.post_create, name="create"),
    path("<uuid:post_id>/", views.post_detail, name="detail"),
    path("<uuid:post_id>/refresh/", views.post_refresh, name="refresh"),
    path("<uuid:post_id>/delete/", views.post_delete, name="delete"),
]
