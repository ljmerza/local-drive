from django.urls import path

from backup import views

app_name = "backup"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("oauth/google/", views.google_auth_start, name="google_auth_start"),
    path(
        "oauth/google/callback/",
        views.google_auth_callback,
        name="google_auth_callback",
    ),
]
