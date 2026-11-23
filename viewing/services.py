from datetime import timedelta
from math import ceil
from uuid import UUID

from django.contrib.sessions.models import Session
from django.core import signing
from django.db import transaction
from django.db.models import Exists, F, OuterRef, Q
from django.utils import timezone

from accounts.models import Device
from accounts.services import locked_user
from billing.services import current_subscription
from catalog.models import Movie, MovieMedia
from viewing.models import PlaybackSession, WatchEvent, WatchHistory

IDLE_LIMIT = timedelta(seconds=120)
INTENT_SALT = "viewing.start.v1"


class PlaybackError(Exception):
    def __init__(self, code, message, status=409):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


def make_intent(device, movie_id, session_id, tab_key, replacement=None):
    return signing.dumps({
        "device": device.pk, "profile": device.active_profile_id, "movie": movie_id,
        "id": str(session_id), "tab": str(tab_key), "replace": str(replacement) if replacement else None,
    }, salt=INTENT_SALT)


def live_sessions(now):
    return PlaybackSession.objects.filter(
        closed_at__isnull=True, last_seen_at__gt=now - IDLE_LIMIT,
        device__revoked_at__isnull=True, device__user__is_active=True,
        device__active_profile_id=F("profile_id"), profile__user_id=F("device__user_id"),
        subscription__user_id=F("device__user_id"), subscription__status="active",
        subscription__start_at__lte=now, subscription__end_at__gt=now, movie__is_published=True,
    ).filter(
        Q(movie__age_rating__lte=12) | Q(profile__is_kids=False, device__kids_locked=False),
        Exists(Session.objects.filter(session_key=OuterRef("device__session_key"), expire_date__gt=now)),
    )


def close_sessions(reason, **filters):
    PlaybackSession.objects.filter(closed_at__isnull=True, **filters).update(
        closed_at=timezone.now(), close_reason=reason,
    )


def device_for(user_id, session_key, now):
    device = Device.objects.select_related("active_profile").filter(
        user_id=user_id, user__is_active=True, session_key=session_key, revoked_at__isnull=True,
        active_profile__user_id=user_id,
    ).filter(Exists(Session.objects.filter(session_key=OuterRef("session_key"), expire_date__gt=now))).first()
    if not device:
        raise PlaybackError("profile", "Войдите и выберите профиль.", 403)
    return device


def movie_access(device, movie_id, *, lock=False, now=None):
    now = now or timezone.now()
    movies = Movie.objects.select_for_update() if lock else Movie.objects.all()
    movie = movies.filter(pk=movie_id, is_published=True).first()
    if not movie or (movie.age_rating > 12 and (device.kids_locked or device.active_profile.is_kids)):
        raise PlaybackError("movie", "Фильм недоступен для этого профиля.", 403)
    period = current_subscription(device.user_id, now)
    if not period:
        raise PlaybackError("subscription", "Для просмотра нужна действующая подписка.", 403)
    return movie, period


def bound_session(user_id, session_key, session_id, now):
    if not user_id or not session_key:
        raise PlaybackError("ended", "Сеанс завершён или доступ отозван.", 403)
    session = live_sessions(now).filter(
        pk=session_id, device__user_id=user_id, device__session_key=session_key,
    ).first()
    if not session:
        raise PlaybackError("ended", "Сеанс завершён или доступ отозван.", 403)
    return session


def video_variants(movie_id):
    return MovieMedia.objects.filter(movie_id=movie_id, kind="video").order_by("variant", "pk")


@transaction.atomic
def cleanup_sessions(now=None):
    now = now or timezone.now()
    retention = timedelta(days=1)
    return PlaybackSession.objects.filter(
        Q(closed_at__lte=now - retention)
        | Q(closed_at__isnull=True, last_seen_at__lte=now - retention - IDLE_LIMIT)
    ).delete()[0]


@transaction.atomic
def start_playback(user_id, session_key, token):
    user = locked_user(user_id)
    now = timezone.now()
    device = device_for(user.pk, session_key, now)
    try:
        intent = signing.loads(token, salt=INTENT_SALT)
        session_id, tab_key = UUID(intent["id"]), UUID(intent["tab"])
    except (signing.BadSignature, KeyError, ValueError, TypeError) as exc:
        raise PlaybackError("intent", "Обновите страницу плеера.", 400) from exc
    if intent["device"] != device.pk or intent["profile"] != device.active_profile_id:
        raise PlaybackError("binding", "Устройство или активный профиль изменились.", 403)
    movie, period = movie_access(device, intent["movie"], lock=True, now=now)
    now = timezone.now()
    device_for(user.pk, session_key, now)
    if period.end_at <= now:
        raise PlaybackError("subscription", "Оплаченный период завершился.", 403)
    bindings = dict(device=device, profile=device.active_profile, movie=movie, tab_key=tab_key)
    existing = PlaybackSession.objects.filter(pk=session_id).first()
    if existing:
        if not live_sessions(now).filter(pk=existing.pk, subscription=period, **bindings).exists():
            raise PlaybackError("ended", "Этот сеанс завершён. Продолжите просмотр.")
        existing.last_seen_at = now
        existing.save(update_fields=["last_seen_at"])
        return existing
    try:
        signing.loads(token, salt=INTENT_SALT, max_age=86400)
    except signing.SignatureExpired as exc:
        raise PlaybackError("intent", "Обновите страницу плеера.") from exc
    if not video_variants(movie.pk).exists():
        raise PlaybackError("media", "Полное видео недоступно.", 404)
    history = WatchHistory.objects.filter(profile=device.active_profile, movie=movie).first()
    if intent["replace"]:
        old = PlaybackSession.objects.filter(pk=intent["replace"], **bindings).first()
        if not old or not history or history.writer_session_id != old.pk:
            raise PlaybackError("writer", "Просмотр уже изменён в другом плеере.")
        close_sessions("restart", pk=old.pk)
    if live_sessions(now).filter(subscription=period).count() >= period.max_streams:
        raise PlaybackError("limit", "Все места заняты. Завершите сеанс в разделе устройств или подождите две минуты.")
    playback = PlaybackSession.objects.create(
        id=session_id, subscription=period, started_at=now, last_seen_at=now, **bindings,
    )
    WatchEvent.objects.create(session=playback, movie=movie, profile=device.active_profile, started_at=now)
    defaults = {"writer_session": playback, "watched_at": now}
    if intent["replace"]:
        defaults.update(progress_sec=0, is_completed=False)
    WatchHistory.objects.update_or_create(profile=device.active_profile, movie=movie, defaults=defaults)
    return playback


@transaction.atomic
def activity(user_id, session_key, session_id, *, sequence=None, position=None, media_id=None):
    locked_user(user_id)
    now = timezone.now()
    playback = bound_session(user_id, session_key, session_id, now)
    Movie.objects.select_for_update().get(pk=playback.movie_id)
    now = timezone.now()
    playback = bound_session(user_id, session_key, session_id, now)
    playback.last_seen_at = now
    playback.save(update_fields=["last_seen_at"])
    history = WatchHistory.objects.get(profile_id=playback.profile_id, movie_id=playback.movie_id)
    writer = history.writer_session_id == playback.pk
    if sequence is None or not writer or sequence <= playback.last_update_seq:
        return writer
    media = video_variants(playback.movie_id).filter(pk=media_id).first()
    if not media or not media.duration or not 0 <= position <= ceil(media.duration):
        raise PlaybackError("position", "Позиция вне длительности фильма.", 400)
    playback.last_update_seq = sequence
    playback.save(update_fields=["last_update_seq"])
    history.progress_sec, history.watched_at = position, now
    if not history.is_completed and position * 100 >= media.duration * 95:
        history.is_completed = True
        WatchEvent.objects.filter(session=playback, completed_at__isnull=True).update(completed_at=now)
    history.save(update_fields=["progress_sec", "watched_at", "is_completed"])
    return True
