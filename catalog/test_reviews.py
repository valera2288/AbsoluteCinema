from datetime import timedelta

from django.contrib.auth.models import Group, Permission
from django.core.management import call_command
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from billing.models import Subscription, Tariff
from catalog.models import Favorite, Genre, Movie, MovieGenre, Review
from operations.models import AuditEntry, Notification
from viewing.models import WatchHistory

PASSWORD = "Review8-study!"
TEXT = "Интересный фильм с неожиданной концовкой."


class ReviewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.author = User.objects.create_user("author", "author@example.test", PASSWORD)
        cls.profile = cls.author.profiles.create(profile_name="Зритель")
        cls.second = cls.author.profiles.create(profile_name="Детский", is_kids=True)
        cls.moderator = User.objects.create_user("moderator", "moderator@example.test")
        call_command("setup_roles")
        cls.moderator.groups.add(Group.objects.get(name="Модераторы"))
        cls.movie = cls.film("Ночной сигнал")

    @staticmethod
    def film(title, age=12):
        return Movie.objects.create(title=title, release_year=2024, duration_min=2, age_rating=age,
                                    description="Учебный фильм", is_published=True, published_at=timezone.now())

    def setUp(self):
        self.assertEqual(self.client.post(reverse("viewer-login"), {
            "username": self.author.login, "password": PASSWORD,
        }).status_code, 302)
        self.mod = Client()
        self.mod.force_login(self.moderator)

    def edit(self, action, version=0, profile=None, **data):
        return self.client.post(reverse("movie-review", args=[self.movie.pk, action]), {
            "version": version, "profile_id": (profile or self.profile).pk, **data,
        })

    def decide(self, review, action, reason="", version=None):
        return self.mod.post(reverse("moderation-decision", args=[review.pk, action]), {
            "version": review.version if version is None else version, "reason": reason,
        })

    def assert_rating(self, count, value):
        movie = self.client.get(reverse("movie-detail", args=[self.movie.pk])).context["movie"]
        self.assertEqual(movie.rating_count, count)
        self.assertEqual(movie.average_rating, value)
        rows = self.client.get(reverse("catalog"), {"min_rating": "4.0"}).context["page"].object_list
        self.assertEqual(self.movie.pk in {row.pk for row in rows}, bool(count and value >= 4))

    def test_rating_pending_rejected_clear_and_delete(self):
        self.assertEqual(self.edit("rate", rating=5).status_code, 302)
        self.assert_rating(1, 5)
        self.assertEqual(self.edit("text", 1, rating=5, text=TEXT).status_code, 302)
        self.assert_rating(0, None)
        review = Review.objects.get()
        detail = reverse("movie-detail", args=[self.movie.pk])
        self.assertNotContains(Client().get(detail), TEXT)
        queue = self.mod.get(reverse("moderation-queue"))
        self.assertContains(queue, TEXT)
        self.assertNotContains(self.mod.get(reverse("moderation-queue"), {"date_from": "2099-01-01"}), TEXT)
        for reason in (" 1234 ", "x" * 501):
            self.assertTrue(self.decide(review, "reject", reason).context["form"].errors)
        self.assertEqual(self.decide(review, "reject", "Нужны аргументы").status_code, 302)
        self.assert_rating(0, None)
        self.assertContains(self.client.get(reverse("viewer-collection", args=["reviews"])), "Нужны аргументы")
        self.assertEqual(self.edit("clear", 3).status_code, 302)
        self.assert_rating(1, 5)
        self.client.post(reverse("viewer-delete"), {"current_password": PASSWORD})
        self.assert_rating(0, None)
        self.assertTrue(AuditEntry.objects.filter(action="review.reject").exists())

    def test_publish_remove_and_stale_moderator_decision(self):
        self.edit("text", rating=4, text=TEXT)
        review = Review.objects.get()
        route = reverse("moderation-decision", args=[review.pk, "publish"])
        self.assertEqual(self.client.post(route, {"version": 1}).status_code, 403)
        self.edit("text", 1, rating=4, text=TEXT + " Добавление.")
        self.assertContains(self.decide(review, "publish", version=1), "Отзыв уже изменился")
        self.assertEqual(self.decide(review, "publish", version=2).status_code, 302)
        self.assert_rating(1, 4)
        self.assertContains(Client().get(reverse("movie-detail", args=[self.movie.pk])), TEXT)
        self.decide(review, "publish", version=2)
        self.assertEqual(Notification.objects.filter(event_key__startswith="review:").count(), 1)
        self.assertTrue(self.decide(review, "remove", "x" * 4, version=3).context["form"].errors)
        self.assertEqual(self.decide(review, "remove", "x" * 500, version=3).status_code, 302)
        self.assert_rating(0, None)

    def test_author_restriction_applies_to_every_profile(self):
        self.edit("text", rating=5, text=TEXT)
        review = Review.objects.get()
        self.decide(review, "publish")
        review.refresh_from_db()
        self.assertEqual(self.mod.post(reverse("moderation-author", args=[review.pk]), {"action": "restrict"}).status_code, 302)
        self.assertEqual(self.edit("text", review.version, rating=3, text=TEXT + " Изменение.").status_code, 403)
        self.client.post(reverse("viewer-profile", args=[self.second.pk, "switch"]))
        self.assertEqual(self.edit("text", profile=self.second, rating=5, text=TEXT).status_code, 403)
        self.assertContains(self.edit("rate", rating=4), "Активный профиль изменился")
        self.assertEqual(self.edit("rate", profile=self.second, rating=4).status_code, 302)
        review.refresh_from_db()
        self.assertEqual((review.text, review.moderation_status), (TEXT, "published"))
        self.assert_rating(2, 4.5)

    def test_favorites_and_recommendations_for_two_profiles(self):
        science = Genre.objects.create(name="Фантастика")
        drama = Genre.objects.create(name="Драма")
        next_science, next_drama = self.film("Орбита"), self.film("Город")
        adult_drama = self.film("Ночной город", 16)
        for movie, genre in ((self.movie, science), (next_science, science), (next_drama, drama), (adult_drama, drama)):
            MovieGenre.objects.create(movie=movie, genre=genre)
        endpoint = reverse("viewer-recommendations")
        self.assertContains(self.client.get(endpoint), "Подборка доступна с подпиской")
        tariff = Tariff.objects.create(name="Тариф", price=100, period_days=30, max_streams=1)
        now = timezone.now()
        Subscription.objects.create(user=self.author, tariff=tariff, status="active", start_at=now, end_at=now + timedelta(days=30),
                                    tariff_name=tariff.name, base_price=100, discount=0, period_days=30, max_streams=1)
        self.assertTrue(self.client.get(endpoint).context["fallback"])
        add = reverse("movie-favorite", args=[self.movie.pk, "add"])
        self.assertEqual(self.client.get(add).status_code, 405)
        self.client.post(add)
        self.client.post(add)
        self.assertEqual(Favorite.objects.count(), 1)
        WatchHistory.objects.create(profile=self.profile, movie=self.movie, progress_sec=24, watched_at=now)
        self.assertEqual([movie.pk for movie in self.client.get(endpoint).context["movies"]], [next_science.pk])
        self.client.post(reverse("viewer-profile", args=[self.second.pk, "switch"]))
        self.assertContains(self.client.get(reverse("viewer-collection", args=["favorites"])), "пока нет записей")
        self.client.post(reverse("movie-favorite", args=[next_drama.pk, "add"]))
        self.assertEqual([movie.pk for movie in self.client.get(endpoint).context["movies"]], [next_drama.pk])
        self.assertEqual(self.client.post(reverse("movie-favorite", args=[adult_drama.pk, "add"])).status_code, 404)
        self.client.post(reverse("movie-favorite", args=[next_drama.pk, "remove"]))
        self.assertTrue(self.client.get(endpoint).context["fallback"])
