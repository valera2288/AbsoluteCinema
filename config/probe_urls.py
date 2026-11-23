from django.urls import path

from config import media_probe

urlpatterns = [
    path("probe/", media_probe.player, name="media-probe"),
    path("probe/video/", media_probe.video, name="media-probe-video"),
]
