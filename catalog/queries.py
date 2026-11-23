from django.db.models import Avg, Count, DecimalField, Exists, F, OuterRef, Prefetch, Q
from django.db.models.functions import Cast, Collate, Lower, Round

from catalog.models import Movie, MovieGenre, MovieMedia, Review

PUBLIC_KINDS = ("poster", "banner", "trailer")
SORTS = {"": ("-published_at", "pk"), "year": ("-release_year", "pk"), "title": ("title", "pk")}


def visible_movies(device):
    movies = Movie.objects.filter(is_published=True)
    if device and (device.kids_locked or not device.active_profile or device.active_profile.is_kids):
        movies = movies.filter(age_rating__lte=12)
    return movies


def published_movies(device=None):
    included = Q(reviews__text="") | Q(reviews__moderation_status=Review.Status.PUBLISHED)
    return visible_movies(device).annotate(
        rating_count=Count("reviews", filter=included),
        average_rating=Round(Avg(Cast("reviews__rating", DecimalField(max_digits=2, decimal_places=0)), filter=included), precision=1),
    ).prefetch_related(
        Prefetch("media", queryset=MovieMedia.objects.filter(kind__in=PUBLIC_KINDS).order_by("kind", "variant"),
                 to_attr="public_media"),
    ).order_by(*SORTS[""])


def filtered_movies(values, device=None):
    movies = published_movies(device)
    if values["q"]:
        movies = movies.alias(search_title=Lower(Collate("title", "pg_c_utf8"))).filter(
            search_title__contains=values["q"].lower(),
        )
    if values["year"]:
        movies = movies.filter(release_year=values["year"])
    if values["genre"]:
        movies = movies.filter(Exists(MovieGenre.objects.filter(
            movie_id=OuterRef("pk"), genre__in=values["genre"], genre__is_active=True,
        )))
    if values["min_rating"] is not None:
        movies = movies.filter(average_rating__gte=values["min_rating"])
    if values["sort"] == "rating":
        return movies.order_by(F("average_rating").desc(nulls_last=True), "pk")
    return movies.order_by(*SORTS[values["sort"]])


def prepare_movie(movie):
    for material in movie.public_media:
        if not hasattr(movie, material.kind):
            setattr(movie, material.kind, material)
    return movie
