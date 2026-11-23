import uuid

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models.functions import Lower, Trim
from django.utils import timezone


def maximum_year():
    return timezone.localdate().year + 1


class Genre(models.Model):
    name = models.CharField("название", max_length=50)
    is_active = models.BooleanField("действующий", default=True)

    class Meta:
        db_table = "genres"
        ordering = ["name", "pk"]
        verbose_name = "жанр"
        verbose_name_plural = "жанры"
        constraints = [models.UniqueConstraint(Lower(Trim("name")), name="unique_genre_name")]

    def __str__(self):
        return self.name


class Movie(models.Model):
    title = models.CharField("название", max_length=150)
    release_year = models.PositiveSmallIntegerField("год", validators=[
        MinValueValidator(1888), MaxValueValidator(maximum_year),
    ])
    duration_min = models.PositiveSmallIntegerField("длительность, мин", validators=[
        MinValueValidator(1), MaxValueValidator(600),
    ])
    age_rating = models.PositiveSmallIntegerField("возраст", choices=[
        (age, f"{age}+") for age in (0, 6, 12, 16, 18)
    ])
    description = models.TextField("описание", max_length=4000)
    is_published = models.BooleanField("опубликован", default=False, editable=False)
    published_at = models.DateTimeField("дата первой публикации", null=True, editable=False)
    genres = models.ManyToManyField(Genre, through="MovieGenre", verbose_name="жанры")

    class Meta:
        db_table = "movies"
        verbose_name = "фильм"
        verbose_name_plural = "фильмы"
        permissions = [("publish_movie", "Может публиковать и снимать фильмы")]
        constraints = [
            models.CheckConstraint(condition=models.Q(duration_min__range=(1, 600)), name="movie_duration_range"),
            models.CheckConstraint(condition=models.Q(age_rating__in=(0, 6, 12, 16, 18)), name="movie_age_rating"),
        ]
        indexes = [
            models.Index(fields=["-published_at", "id"], condition=models.Q(is_published=True), name="movies_public_date"),
            models.Index(fields=["release_year", "id"], condition=models.Q(is_published=True), name="movies_public_year"),
        ]

    def __str__(self):
        return self.title


class MovieGenre(models.Model):
    movie = models.ForeignKey(Movie, on_delete=models.CASCADE, db_index=False, verbose_name="фильм")
    genre = models.ForeignKey(Genre, on_delete=models.PROTECT, related_name="movies_via_link", verbose_name="жанр")

    class Meta:
        db_table = "movie_genres"
        constraints = [models.UniqueConstraint(fields=["movie", "genre"], name="unique_movie_genre")]


class MovieMedia(models.Model):
    class Kind(models.TextChoices):
        POSTER = "poster", "Постер"
        BANNER = "banner", "Обложка"
        TRAILER = "trailer", "Трейлер"
        VIDEO = "video", "Полное видео"
        SUBTITLE = "subtitle", "Субтитры"

    movie = models.ForeignKey(Movie, on_delete=models.CASCADE, related_name="media", db_index=False)
    kind = models.CharField("вид", max_length=12, choices=Kind.choices)
    variant = models.SlugField("вариант", max_length=32, default="default")
    storage_key = models.CharField(max_length=64, unique=True, editable=False)
    version_id = models.UUIDField(default=uuid.uuid4, editable=False)
    file_format = models.CharField(max_length=8, editable=False)
    size = models.PositiveBigIntegerField(editable=False)
    duration = models.DecimalField(max_digits=12, decimal_places=6, null=True, editable=False)
    allow_download = models.BooleanField("разрешено скачивание", default=False)

    class Meta:
        db_table = "movie_media"
        verbose_name = "медиафайл"
        verbose_name_plural = "медиафайлы"
        constraints = [
            models.UniqueConstraint(fields=["movie", "kind", "variant"], name="unique_movie_media_variant"),
            models.CheckConstraint(condition=models.Q(size__gt=0), name="movie_media_positive_size"),
            models.CheckConstraint(condition=models.Q(kind="video") | models.Q(allow_download=False), name="movie_media_download_video"),
            models.CheckConstraint(condition=~models.Q(kind__in=("trailer", "banner")) | models.Q(variant="default"), name="movie_media_fixed_variant"),
        ]


class Favorite(models.Model):
    profile = models.ForeignKey("accounts.Profile", on_delete=models.CASCADE)
    movie = models.ForeignKey(Movie, on_delete=models.CASCADE)
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "favorites"
        constraints = [models.UniqueConstraint(fields=["profile", "movie"], name="favorite_profile_movie")]
        indexes = [models.Index(fields=["profile", "-added_at"], name="favorite_recent")]


class Review(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "На модерации"
        PUBLISHED = "published", "Опубликован"
        REJECTED = "rejected", "Отклонён"
        REMOVED = "removed", "Снят с публикации"

    profile = models.ForeignKey("accounts.Profile", on_delete=models.CASCADE)
    movie = models.ForeignKey(Movie, on_delete=models.CASCADE, related_name="reviews")
    rating = models.PositiveSmallIntegerField()
    text = models.TextField(max_length=2000, blank=True)
    moderation_status = models.CharField(max_length=10, choices=Status.choices, null=True, blank=True)
    reason = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    submitted_at = models.DateTimeField(null=True)
    version = models.PositiveBigIntegerField(default=1)

    class Meta:
        db_table = "reviews"
        permissions = [
            ("view_review_queue", "Может просматривать очередь отзывов"),
            ("decide_review", "Может публиковать и отклонять отзывы"),
            ("remove_review", "Может снимать отзывы с публикации"),
        ]
        constraints = [
            models.UniqueConstraint(fields=["profile", "movie"], name="review_profile_movie"),
            models.CheckConstraint(condition=models.Q(rating__range=(1, 5)), name="review_rating_range"),
            models.CheckConstraint(condition=(
                models.Q(text="", moderation_status__isnull=True, submitted_at__isnull=True, reason="")
                | (~models.Q(text="") & models.Q(submitted_at__isnull=False, moderation_status__isnull=False)
                   & (models.Q(moderation_status__in=["pending", "published"], reason="")
                      | (models.Q(moderation_status__in=["rejected", "removed"]) & ~models.Q(reason=""))))
            ), name="review_moderation_consistency"),
        ]
        indexes = [
            models.Index(fields=["moderation_status", "submitted_at", "id"], name="review_queue"),
            models.Index(fields=["movie", "moderation_status"], name="review_movie_status"),
        ]
