from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from catalog.media import verify_media
from catalog.models import Genre, Movie, MovieMedia
from operations.models import AuditEntry


@transaction.atomic
def set_publication(actor, movie_id, publish):
    if not actor.is_active or not actor.is_staff or not actor.has_perm("catalog.publish_movie"):
        raise PermissionDenied
    movie = Movie.objects.select_for_update().get(pk=movie_id)
    if movie.is_published == publish:
        return False
    if publish:
        genres = list(Genre.objects.select_for_update().filter(movies_via_link__movie=movie).order_by("pk"))
        if not any(genre.is_active for genre in genres):
            raise ValidationError("Добавьте действующий жанр.")
        materials = list(movie.media.all())
        if not {MovieMedia.Kind.POSTER, MovieMedia.Kind.TRAILER, MovieMedia.Kind.VIDEO}.issubset(
            {material.kind for material in materials}
        ):
            raise ValidationError("Для публикации нужны постер, трейлер и полное видео.")
        for material in materials:
            verify_media(material)
        durations = [material.duration for material in materials if material.kind == "video"]
        if max(durations) - min(durations) > 1:
            raise ValidationError("Длительности вариантов полного видео не совпадают.")
    movie.is_published = publish
    if publish and movie.published_at is None:
        movie.published_at = timezone.now()
    movie.save(update_fields=["is_published", "published_at"])
    if not publish:
        from viewing.services import close_sessions

        close_sessions("movie_hidden", movie=movie)
    AuditEntry.objects.create(
        actor=actor, action="movie.publish" if publish else "movie.unpublish",
        object_kind="movie", object_ref=str(movie.pk), result_code="ok",
    )
    return True
