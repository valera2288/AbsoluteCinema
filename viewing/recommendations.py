from django.db.models import Count, Exists, F, OuterRef, Q, Subquery

from catalog.models import Favorite, MovieGenre
from catalog.queries import published_movies
from viewing.models import WatchHistory


def recommendations(device):
    profile_id = device.active_profile_id
    history = WatchHistory.objects.filter(profile_id=profile_id, progress_sec__gt=0)
    favorites = Favorite.objects.filter(profile_id=profile_id)
    interests = MovieGenre.objects.filter(genre__is_active=True).filter(
        Q(movie_id__in=history.values("movie_id")) | Q(movie_id__in=favorites.values("movie_id")),
    ).order_by().values("genre_id")
    available = published_movies(device).filter(~Exists(history.filter(movie_id=OuterRef("pk"))))
    matches = MovieGenre.objects.filter(movie_id=OuterRef("pk"), genre_id__in=interests).order_by().values("movie_id")
    matches = matches.annotate(score=Count("genre_id")).values("score")
    ranked = available.annotate(interest_score=Subquery(matches)).filter(interest_score__gt=0).order_by(
        "-interest_score", F("average_rating").desc(nulls_last=True), "-published_at", "pk",
    )
    movies = list(ranked[:12])
    return (movies, False) if movies else (list(available[:12]), True)
