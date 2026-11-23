from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import parse_qs

from django.conf import settings
from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from billing.models import Tariff
from catalog.media import inspect_mp4
from catalog.models import Genre, Movie, MovieGenre, MovieMedia, Review
from catalog.services import set_publication
from operations.models import AuditEntry


class CatalogTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser("cinemaadmin", "cinemaadmin@example.test")
        cls.staff = User.objects.create_user("cinemastaff", "cinemastaff@example.test", is_staff=True)
        cls.genre = Genre.objects.create(name="Фантастика")
        cls.movie = Movie.objects.create(title="Ночной сигнал", release_year=2026, duration_min=2, age_rating=12, description="История о радиосигнале.")
        MovieGenre.objects.create(movie=cls.movie, genre=cls.genre)

    def test_admin_creates_draft_and_assigns_genre(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("admin:catalog_movie_add"), {
            "title": "Новый черновик", "release_year": 2026, "duration_min": 2,
            "age_rating": 12, "description": "Описание", "genres": [self.genre.pk], "is_published": "on",
        })
        self.assertEqual(response.status_code, 302)
        movie = Movie.objects.get(title="Новый черновик")
        self.assertFalse(movie.is_published)
        self.assertEqual(movie.genres.get(), self.genre)
        self.assertEqual(self.client.get(reverse("movie-detail", args=[movie.pk])).status_code, 404)

    def test_permissions_and_incomplete_publication(self):
        action = reverse("admin:catalog_movie_publication", args=[self.movie.pk])
        self.client.force_login(self.staff)
        self.assertEqual(self.client.post(reverse("admin:catalog_movie_add"), {}).status_code, 403)
        self.staff.user_permissions.add(Permission.objects.get(codename="change_movie"))
        self.assertEqual(self.client.post(action, {"action": "publish"}).status_code, 403)
        self.client.force_login(self.admin)
        self.assertContains(self.client.post(action, {"action": "publish"}), "нужны постер, трейлер", status_code=400)
        self.movie.refresh_from_db()
        self.assertFalse(self.movie.is_published)
        self.assertFalse(AuditEntry.objects.exists())

    def test_catalog_validation_filter_or_and_pagination(self):
        other = Genre.objects.create(name="Драма")
        profile = self.staff.profiles.create(profile_name="Зритель")
        for number in range(14):
            film = Movie.objects.create(title=f"Сигнал {number}", release_year=2026, duration_min=2, age_rating=12,
                                        description="Демо", is_published=True, published_at=timezone.now())
            MovieGenre.objects.create(movie=film, genre=other if number % 2 else self.genre)
            Review.objects.create(profile=profile, movie=film, rating=5)
        endpoint = reverse("catalog")
        for field, value in (("year", "abc"), ("year", "1887"), ("year", "2200"), ("genre", "999999"),
                             ("min_rating", "99"), ("q", "x" * 101), ("sort", "bad")):
            with self.subTest(field=field, value=value):
                self.assertIn(field, self.client.get(endpoint, {field: value}).context["form"].errors)
        self.assertContains(self.client.get(endpoint, {"q": "Не существует"}), "Фильмы не найдены")
        query = {"q": "сИгНаЛ", "year": 2026, "genre": [self.genre.pk, other.pk], "sort": "title", "min_rating": "4.5"}
        response = self.client.get(endpoint, query)
        self.assertEqual(response.context["page"].paginator.count, 14)
        self.assertNotContains(response, self.movie.title)
        _, next_query = response.context["links"][0]
        parsed = parse_qs(next_query)
        self.assertEqual(parsed["genre"], [str(self.genre.pk), str(other.pk)])
        self.assertEqual(parsed["q"], [query["q"]])
        self.assertEqual(parsed["min_rating"], ["4.5"])
        self.assertEqual(parsed["page"], ["2"])
        self.assertEqual(len(self.client.get(f"{endpoint}?{next_query}").context["page"].object_list), 2)

    def test_only_active_tariff_visible(self):
        Tariff.objects.create(name="Открытый", price=299, period_days=30, max_streams=2)
        Tariff.objects.create(name="Отключённый", price=499, period_days=60, max_streams=4, is_active=False)
        response = self.client.get(reverse("tariffs"))
        self.assertContains(response, "Открытый")
        self.assertContains(response, "299,00")
        self.assertNotContains(response, "Отключённый")

    def test_media_workflow_publication_rollback_and_revocation(self):
        with TemporaryDirectory() as temporary, override_settings(CATALOG_MEDIA_ROOT=Path(temporary) / "catalog"):
            from django.core.management import call_command
            call_command("prepare_catalog_demo", Path(temporary) / "art")
            mp4 = (settings.BASE_DIR / "private" / "online-cinema-probe.mp4").read_bytes()
            post = (Path(temporary) / "art" / "poster.jpg").read_bytes()
            self.client.force_login(self.admin)
            for kind, name, data, mime in (("poster", "poster.jpg", post, "image/jpeg"),
                                            ("trailer", "trailer.mp4", mp4, "video/mp4"),
                                            ("video", "movie.mp4", mp4, "video/mp4")):
                response = self.client.post(reverse("admin:catalog_moviemedia_add"), {
                    "movie": self.movie.pk, "kind": kind, "variant": "default",
                    "upload": SimpleUploadedFile(name, data, content_type=mime),
                })
                self.assertEqual(response.status_code, 302, response.context["adminform"].form.errors if response.context else "")
            with patch("catalog.services.AuditEntry.objects.create", side_effect=IntegrityError):
                with self.assertRaises(IntegrityError):
                    set_publication(self.admin, self.movie.pk, True)
            self.movie.refresh_from_db()
            self.assertFalse(self.movie.is_published)
            action = reverse("admin:catalog_movie_publication", args=[self.movie.pk])
            self.assertEqual(self.client.post(action, {"action": "publish"}).status_code, 302)
            self.assertEqual(self.client.post(action, {"action": "publish"}).status_code, 302)
            self.assertEqual(AuditEntry.objects.filter(action="movie.publish").count(), 1)
            self.movie.refresh_from_db()
            published_at = self.movie.published_at
            detail = reverse("movie-detail", args=[self.movie.pk])
            guest = self.client_class()
            self.assertContains(guest.get(reverse("catalog"), {"q": "сИГНАЛ"}), self.movie.title)
            self.assertContains(guest.get(detail), "Трейлер")
            trailer = self.movie.media.get(kind="trailer")
            movie_url = reverse("movie-media", args=[self.movie.pk, trailer.pk, trailer.version_id])
            response = guest.get(movie_url, HTTP_RANGE="bytes=0-15")
            self.assertEqual(response.status_code, 206)
            self.assertEqual(b"".join(response.streaming_content), mp4[:16])
            self.assertEqual(response["Content-Range"], f"bytes 0-15/{len(mp4)}")
            self.assertEqual(guest.get(movie_url, HTTP_RANGE=f"bytes={len(mp4)}-").status_code, 416)
            with self.captureOnCommitCallbacks(execute=True):
                updated = self.client.post(reverse("admin:catalog_moviemedia_change", args=[trailer.pk]), {
                    "upload": SimpleUploadedFile("replacement.mp4", mp4, content_type="video/mp4"),
                })
            self.assertEqual(updated.status_code, 302)
            old_key, old_version = trailer.storage_key, trailer.version_id
            trailer.refresh_from_db()
            self.assertNotEqual(trailer.storage_key, old_key)
            self.assertNotEqual(trailer.version_id, old_version)
            self.assertFalse((settings.CATALOG_MEDIA_ROOT / old_key).exists())
            self.assertEqual(guest.get(movie_url).status_code, 410)
            video = self.movie.media.get(kind="video")
            self.assertEqual(guest.get(reverse("movie-media", args=[self.movie.pk, video.pk, video.version_id])).status_code, 404)
            draft = Movie.objects.create(title="Черновик", release_year=2026, duration_min=2, age_rating=12, description="Демо")
            self.assertEqual(guest.get(reverse("movie-detail", args=[draft.pk])).status_code, 404)
            self.assertEqual(self.client.post(action, {"action": "unpublish"}).status_code, 302)
            self.movie.refresh_from_db()
            self.assertEqual(self.movie.published_at, published_at)
            self.assertTrue(AuditEntry.objects.filter(action="movie.unpublish", actor=self.admin).exists())
            self.assertEqual(guest.get(detail).status_code, 404)
            self.assertEqual(guest.get(reverse("movie-media", args=[self.movie.pk, trailer.pk, trailer.version_id])).status_code, 404)

    def test_mp4_rejects_actual_audio_only_and_wrong_codec(self):
        self.client.force_login(self.admin)
        for name, message in (("audio-only", "H.264"), ("wrong-codec", "H.264"), ("wrong-audio", "AAC")):
            with self.subTest(name=name):
                path = settings.BASE_DIR / "private" / "catalog-test-media" / f"{name}.mp4"
                with self.assertRaisesMessage(ValidationError, message):
                    inspect_mp4(path)
                response = self.client.post(reverse("admin:catalog_moviemedia_add"), {
                    "movie": self.movie.pk, "kind": "video", "variant": name,
                    "upload": SimpleUploadedFile(path.name, path.read_bytes(), content_type="video/mp4"),
                })
                self.assertIn(message, str(response.context["adminform"].form.errors["upload"]))
        self.assertFalse(self.movie.media.exists())
