from datetime import timedelta

from django.contrib.auth import logout
from django.contrib.sessions.models import Session
from django.db.models import Exists, OuterRef
from django.shortcuts import redirect
from django.utils import timezone

from accounts.models import Device


class ViewerDeviceMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.viewer_device = None
        if request.user.is_authenticated and request.session.get("viewer_device"):
            now = timezone.now()
            device = Device.objects.select_related("active_profile").filter(
                user_id=request.user.pk, session_key=request.session.session_key, revoked_at__isnull=True,
            ).filter(Exists(Session.objects.filter(session_key=OuterRef("session_key"), expire_date__gt=now))).first()
            if device is None:
                logout(request)
                return redirect("viewer-login")
            request.viewer_device = device
            Device.objects.filter(pk=device.pk, revoked_at__isnull=True,
                                  last_seen_at__lt=now - timedelta(minutes=1)).update(last_seen_at=now)
        return self.get_response(request)
