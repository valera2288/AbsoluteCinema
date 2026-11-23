from django.urls import path

from operations import views

urlpatterns = [
    path("support/", views.support, name="support"),
    path("support/<int:ticket_id>/", views.support_detail, name="support-detail"),
    path("notifications/", views.inbox, name="notification-inbox"),
    path("notifications/<int:notification_id>/read/", views.mark_read, name="notification-read"),
]
