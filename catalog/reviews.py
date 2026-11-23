from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone

from accounts.models import User
from accounts.services import locked_user
from billing.services import notify
from catalog.models import Favorite, Movie, Review
from operations.models import AuditEntry
from viewing.services import device_for

STATUS = Review.Status
DECISIONS = {
    "publish": ("catalog.decide_review", STATUS.PENDING, STATUS.PUBLISHED),
    "reject": ("catalog.decide_review", STATUS.PENDING, STATUS.REJECTED),
    "remove": ("catalog.remove_review", STATUS.PUBLISHED, STATUS.REMOVED),
}


def profile_and_movie(request, movie_id):
    user = locked_user(request.user.pk)
    device = device_for(user.pk, request.session.session_key, timezone.now())
    movies = Movie.objects.select_for_update().filter(is_published=True)
    if device.kids_locked or device.active_profile.is_kids:
        movies = movies.filter(age_rating__lte=12)
    return user, device.active_profile, get_object_or_404(movies, pk=movie_id)


@transaction.atomic
def favorite(request, movie_id, add):
    _, profile, movie = profile_and_movie(request, movie_id)
    if add:
        Favorite.objects.get_or_create(profile=profile, movie=movie)
    else:
        Favorite.objects.filter(profile=profile, movie=movie).delete()


@transaction.atomic
def save_review(request, movie_id, action, version, profile_id, rating=None, text=""):
    user, profile, movie = profile_and_movie(request, movie_id)
    if profile.pk != profile_id:
        raise ValidationError("Активный профиль изменился. Откройте форму заново.")
    if action == "text":
        if not user.can_post_reviews:
            raise PermissionDenied
        text = text.strip()
        if not 10 <= len(text) <= 2000:
            raise ValidationError("Текст отзыва должен содержать от 10 до 2000 символов.")
    review = Review.objects.select_for_update().filter(profile=profile, movie=movie).first()
    if version != (review.version if review else 0):
        raise ValidationError("Запись изменилась. Откройте форму заново.")
    if action == "clear" and (not review or not review.text):
        return
    if action not in ("rate", "text", "clear") or (action != "clear" and rating not in range(1, 6)):
        raise ValidationError("Укажите оценку от 1 до 5.")
    if not review:
        review = Review(profile=profile, movie=movie)
    else:
        review.version += 1
    if action != "clear":
        review.rating = rating
    if action in ("text", "clear"):
        review.text = text if action == "text" else ""
        review.moderation_status = STATUS.PENDING if action == "text" else None
        review.submitted_at = timezone.now() if action == "text" else None
        review.reason = ""
    review.save()


def lock_people(actor_id, owner_id):
    people = {person.pk: person for person in User.objects.select_for_update().filter(
        pk__in=[actor_id, owner_id],
    ).order_by("pk")}
    if actor_id not in people or owner_id not in people or not people[actor_id].is_active:
        raise PermissionDenied
    return people[actor_id], people[owner_id]


@transaction.atomic
def decide(actor_id, review_id, action, version, reason=""):
    permission, before, after = DECISIONS[action]
    owner_id = get_object_or_404(Review.objects.select_related("profile"), pk=review_id).profile.user_id
    actor, owner = lock_people(actor_id, owner_id)
    if not actor.has_perm("catalog.view_review_queue") or not actor.has_perm(permission):
        raise PermissionDenied
    review = get_object_or_404(Review.objects.select_for_update(), pk=review_id, profile__user=owner)
    if review.version != version or review.moderation_status != before:
        raise ValidationError("Отзыв уже изменился. Обновите очередь.")
    reason = reason.strip() if action != "publish" else ""
    if action != "publish" and not 5 <= len(reason) <= 500:
        raise ValidationError("Укажите причину длиной от 5 до 500 символов.")
    review.moderation_status, review.reason = after, reason
    review.version += 1
    review.save(update_fields=["moderation_status", "reason", "version", "updated_at"])
    AuditEntry.objects.create(actor=actor, action=f"review.{action}", object_kind="review",
                              object_ref=str(review.pk), result_code="ok")
    notify(owner, f"review:{review.pk}:{review.version}", "Решение по отзыву",
           f"Новый статус: {review.get_moderation_status_display()}. Причина и детали доступны в кабинете.")


@transaction.atomic
def restrict_author(actor_id, review_id, allow, expected):
    owner_id = get_object_or_404(Review.objects.select_related("profile"), pk=review_id).profile.user_id
    actor, owner = lock_people(actor_id, owner_id)
    if not actor.has_perm("catalog.view_review_queue") or not actor.has_perm("accounts.restrict_review_authors"):
        raise PermissionDenied
    get_object_or_404(Review, pk=review_id, profile__user=owner)
    if owner.can_post_reviews != expected or owner.can_post_reviews == allow:
        return
    owner.can_post_reviews = allow
    owner.save(update_fields=["can_post_reviews"])
    AuditEntry.objects.create(actor=actor, action="review.author_allowed" if allow else "review.author_restricted",
                              object_kind="review", object_ref=str(review_id), result_code="ok")
