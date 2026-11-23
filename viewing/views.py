import os
from functools import wraps
from uuid import UUID

from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST, require_safe
from django.contrib.auth.views import redirect_to_login

from catalog.media import media_path
from catalog.models import MovieMedia
from catalog.queries import visible_movies
from catalog.queries import prepare_movie
from billing.services import current_subscription
from config.media_probe import video_response
from viewing import services
from viewing.models import WatchHistory
from viewing.recommendations import recommendations


@never_cache
@require_safe
def recommendation_feed(request):
    device = request.viewer_device
    if not device:
        return redirect_to_login(request.get_full_path())
    entitled = bool(device.active_profile_id and current_subscription(request.user.pk))
    movies, fallback = recommendations(device) if entitled else ([], False)
    return render(request, "viewing/recommendations.html", {
        "movies": [prepare_movie(movie) for movie in movies], "fallback": fallback, "entitled": entitled,
    })


def api(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated or not request.viewer_device:
            return JsonResponse({"code": "ended", "message": "Войдите заново."}, status=403)
        try:
            return view(request, *args, **kwargs)
        except services.PlaybackError as exc:
            return JsonResponse({"code": exc.code, "message": exc.message}, status=exc.status)
        except Http404:
            return JsonResponse({"code": "ended", "message": "Сеанс недоступен."}, status=404)
        except (ValueError, KeyError):
            return JsonResponse({"code": "invalid", "message": "Некорректный запрос плеера."}, status=400)
    return never_cache(require_POST(wrapped))


def session_args(request):
    return request.user.pk, request.session.session_key


@never_cache
@require_safe
def player(request, movie_id):
    if not request.user.is_authenticated or not request.viewer_device:
        return redirect_to_login(request.get_full_path())
    movie = get_object_or_404(visible_movies(request.viewer_device), pk=movie_id)
    return render(request, "viewing/player.html", {"movie": movie})


@api
def intent(request, movie_id):
    session_id = UUID(request.POST["id"])
    tab_key = UUID(request.POST["tab"])
    replacement = UUID(request.POST["replace"]) if request.POST.get("replace") else None
    return JsonResponse({"token": services.make_intent(request.viewer_device, movie_id, session_id, tab_key, replacement)})


def payload(session):
    history = WatchHistory.objects.get(profile_id=session.profile_id, movie_id=session.movie_id)
    materials = MovieMedia.objects.filter(movie_id=session.movie_id, kind__in=["video", "subtitle"]).order_by("variant", "pk")
    variants = {"video": [], "subtitle": []}
    for item in materials:
        variants[item.kind].append({"id": item.pk, "label": item.variant,
            "url": reverse("watch-media", args=[session.pk, item.pk, item.version_id])})
    return JsonResponse({"id": session.pk, "position": history.progress_sec, "seq": session.last_update_seq,
                         "activity": reverse("watch-activity", args=[session.pk]),
                         "refresh": reverse("watch-refresh", args=[session.pk]),
                         "writer": history.writer_session_id == session.pk, **variants})


@api
def start(request):
    session = services.start_playback(*session_args(request), request.POST["token"])
    return payload(session)


@api
def refresh(request, session_id):
    services.activity(*session_args(request), session_id)
    return payload(services.bound_session(*session_args(request), session_id, timezone.now()))


@api
def activity(request, session_id):
    values = {}
    if "seq" in request.POST:
        sequence, position, media_id = (int(request.POST[field]) for field in ("seq", "position", "media"))
        if not 0 < sequence <= 2**53 - 1 or not 0 <= position <= 2**31 - 1 or not 0 < media_id <= 2**63 - 1:
            raise ValueError
        values = {"sequence": sequence, "position": position, "media_id": media_id}
    return JsonResponse({"writer": services.activity(*session_args(request), session_id, **values)})


def open_material(material):
    try:
        source = media_path(material.storage_key).open("rb")
    except (ValidationError, OSError):
        raise Http404 from None
    if os.fstat(source.fileno()).st_size != material.size:
        source.close()
        raise Http404
    return source


@never_cache
@require_safe
def media(request, session_id, media_id, version):
    args = (*session_args(request), session_id)
    try:
        playback = services.bound_session(*args, timezone.now())
    except services.PlaybackError:
        return HttpResponse(status=403)
    material = get_object_or_404(MovieMedia, pk=media_id, movie_id=playback.movie_id, kind__in=["video", "subtitle"])
    if material.version_id != version:
        return HttpResponse(status=410)
    source = open_material(material)
    if material.kind == "subtitle":
        return FileResponse(source, content_type="text/vtt", headers={"X-Content-Type-Options": "nosniff"})

    def authorized():
        try:
            services.bound_session(*args, timezone.now())
        except services.PlaybackError:
            return False
        return True

    response = video_response(request, source, material.size, authorized)
    response["X-Content-Type-Options"] = "nosniff"
    return response


@never_cache
@require_safe
def download(request, movie_id, media_id):
    try:
        device = services.device_for(*session_args(request), timezone.now())
        services.movie_access(device, movie_id)
    except services.PlaybackError:
        return HttpResponse(status=403)
    material = get_object_or_404(MovieMedia, pk=media_id, movie_id=movie_id, kind="video", allow_download=True)
    return FileResponse(open_material(material), as_attachment=True, filename=f"film-{movie_id}-{material.variant}.mp4",
                        content_type="video/mp4", headers={"X-Content-Type-Options": "nosniff"})


@never_cache
@require_safe
def history(request):
    device = request.viewer_device
    if not device:
        return redirect_to_login(request.get_full_path())
    rows = WatchHistory.objects.filter(
        profile_id=device.active_profile_id, profile__user_id=request.user.pk, movie__in=visible_movies(device),
    ).select_related("movie").order_by("-watched_at", "-pk")
    return render(request, "viewing/history.html", {"page": Paginator(rows, 24).get_page(request.GET.get("page"))})
