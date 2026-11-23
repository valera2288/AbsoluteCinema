import os
import re
import stat
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import Permission
from django.contrib.sessions.models import Session
from django.contrib.staticfiles import finders
from django.http import HttpResponse, StreamingHttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe

MIB = 1024 * 1024
RANGE = re.compile(r"bytes=([0-9]*)-([0-9]*)", re.IGNORECASE)


def may_watch(user_id, session_key):
    if user_id is None or not session_key:
        return False
    return Session.objects.filter(session_key=session_key, expire_date__gt=timezone.now()).exists() and (
        Permission.objects.filter(
            codename="view_media_probe", content_type__app_label="accounts",
            content_type__model="user", group__user__pk=user_id, group__user__is_active=True,
        ).exists()
    )


@never_cache
@require_safe
def player(request):
    if not may_watch(request.user.pk, request.session.session_key):
        return HttpResponse(status=403)
    return render(request, "media_probe.html")


def open_probe():
    configured_path = Path(settings.MEDIA_PROBE_FILE)
    if not configured_path.is_absolute() or configured_path.suffix.lower() != ".mp4":
        raise FileNotFoundError
    path = configured_path.resolve(strict=True)
    public_roots = [settings.STATIC_ROOT]
    for finder in finders.get_finders():
        public_roots.extend(storage.location for storage in finder.storages.values())
    for root in filter(None, public_roots):
        public_path = Path(os.path.abspath(root))
        if Path(os.path.abspath(configured_path)).is_relative_to(public_path) or path.is_relative_to(public_path.resolve()):
            raise FileNotFoundError
    if not path.is_file():
        raise FileNotFoundError
    source = path.open("rb")
    try:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or not 5 * MIB <= info.st_size <= 20 * MIB:
            raise FileNotFoundError
        return source, info.st_size
    except BaseException:
        source.close()
        raise


def range_number(value, size):
    digits = value.lstrip("0")
    return size + 1 if len(digits) > len(str(size)) else int(digits or "0")


class VideoStream:
    def __init__(self, source, offset, length, authorize):
        self.source = source
        self.offset = offset
        self.length = length
        self.authorize = authorize

    def __iter__(self):
        self.source.seek(self.offset)
        remaining = self.length
        while remaining and self.authorize():
            chunk = self.source.read(min(MIB, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk

    def close(self):
        self.source.close()


@never_cache
@require_safe
def video(request):
    user_id = request.user.pk
    session_key = request.session.session_key
    if not may_watch(user_id, session_key):
        return HttpResponse(status=403)
    try:
        source, size = open_probe()
    except (OSError, RuntimeError, ValueError):
        return HttpResponse(status=404)
    return video_response(request, source, size, lambda: may_watch(user_id, session_key))


def video_response(request, source, size, authorize):
    headers = {"Accept-Ranges": "bytes"}
    start, end, status = 0, size - 1, 200
    header = request.headers.get("Range") if request.method == "GET" else None
    if header is not None:
        match = RANGE.fullmatch(header.strip())
        if not match or not any(match.groups()):
            source.close()
            return HttpResponse(status=400)
        first, last = match.groups()
        if first:
            start = range_number(first, size)
            if last:
                end = min(end, range_number(last, size))
        else:
            start = max(0, size - range_number(last, size))
        if start > end:
            source.close()
            headers["Content-Range"] = f"bytes */{size}"
            return HttpResponse(status=416, headers=headers)
        status = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"

    length = end - start + 1
    headers["Content-Length"] = str(length)
    if request.method == "HEAD":
        source.close()
        return HttpResponse(content_type="video/mp4", headers=headers)
    stream = VideoStream(source, start, length, authorize)
    return StreamingHttpResponse(stream, status=status, content_type="video/mp4", headers=headers)
