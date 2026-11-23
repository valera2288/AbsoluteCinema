from django.urls import path
from django.views.generic import TemplateView

from accounts import views

urlpatterns = [
    path("", views.dashboard, name="viewer-dashboard"),
    path("register/", views.register, name="viewer-register"),
    path("login/", views.ViewerLogin.as_view(), name="viewer-login"),
    path("logout/", views.viewer_logout, name="viewer-logout"),
    path("details/", views.details, name="viewer-details"),
    path("password/", views.password_change, name="viewer-password"),
    path("delete/", views.account_delete, name="viewer-delete"),
    path("reset/", views.ResetRequest.as_view(), name="viewer-reset"),
    path("reset/sent/", TemplateView.as_view(template_name="accounts/reset_sent.html"), name="viewer-reset-sent"),
    path("reset/<uidb64>/<token>/", views.ResetConfirm.as_view(), name="viewer-reset-confirm"),
    path("devices/<int:device_id>/revoke/", views.device_revoke, name="viewer-revoke"),
    path("profiles/create/", views.profiles, {"action": "create"}, name="viewer-profile-create"),
    path("profiles/<int:profile_id>/<str:action>/", views.profiles, name="viewer-profile"),
]
