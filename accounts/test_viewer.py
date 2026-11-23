from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth.hashers import check_password
from django.contrib.auth.tokens import default_token_generator
from django.contrib.sessions.models import Session
from django.conf import settings
from django.core import mail
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from accounts.models import Device, LoginThrottle, Profile, User
from catalog.models import Movie, MovieMedia
from operations.models import AuditEntry

PASSWORD = "Film5-viewer-pass!"
NEW_PASSWORD = "Scene9-new-key!"


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class ViewerTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("viewer", "viewer@example.test", PASSWORD)
        cls.adult = cls.user.profiles.create(profile_name="Основной")
        cls.other = User.objects.create_user("another", "another@example.test", PASSWORD)
        cls.foreign = cls.other.profiles.create(profile_name="Чужой")

    def login_viewer(self, client=None, identifier="VIEWER", password=PASSWORD, **headers):
        client = self.client if client is None else client
        return client.post(reverse("viewer-login"), {"username": identifier, "password": password}, **headers)

    def profile(self, profile, action, data=None, client=None):
        client = self.client if client is None else client
        return client.post(reverse("viewer-profile", args=[profile.pk, action]), data or {})

    def test_registration_and_account_details_casefold_duplicates(self):
        self.assertContains(self.client.get(reverse("viewer-login")), settings.SITE_NAME)
        data = {"login": "CinemaFan", "email": "Fan@Example.test", "password1": PASSWORD, "password2": PASSWORD}
        self.assertRedirects(self.client.post(reverse("viewer-register"), data), reverse("viewer-login"))
        created = User.objects.get(login="cinemafan")
        self.assertEqual(created.email, "fan@example.test")
        self.assertFalse(created.profiles.get().is_kids)
        taken = self.client.post(reverse("viewer-register"), {**data, "login": "VIEWER", "email": "VIEWER@example.test"})
        self.assertEqual(set(taken.context["form"].errors), {"login", "email"})
        self.login_viewer()
        details = self.client.post(reverse("viewer-details"), {"login": "ANOTHER", "email": "ANOTHER@example.test"})
        self.assertEqual(set(details.context["form"].errors), {"login", "email"})
        self.assertEqual(self.client.post(reverse("viewer-details"), {
            "login": "Viewer_2", "email": "New@example.test", "full_name": "Зритель",
        }).status_code, 302)
        self.user.refresh_from_db()
        self.assertEqual((self.user.login, self.user.email), ("viewer_2", "new@example.test"))

    def test_login_pair_lock_and_ip_limit(self):
        for _ in range(5):
            self.assertEqual(self.login_viewer(password="invalid").status_code, 200)
        self.assertEqual(self.login_viewer(identifier="viewer").status_code, 200)
        self.assertFalse(self.user.devices.exists())
        now = timezone.now() + timedelta(minutes=16)
        with patch("accounts.throttle.timezone.now", return_value=now):
            self.assertEqual(self.login_viewer().status_code, 302)
        ip = {"REMOTE_ADDR": "192.0.2.7"}
        for number in range(30):
            self.login_viewer(Client(), identifier=f"missing{number}", **ip)
        self.assertEqual(self.login_viewer(Client(), **ip).status_code, 200)
        self.assertEqual(self.login_viewer(Client(), identifier="VIEWER@EXAMPLE.TEST",
                                           REMOTE_ADDR="192.0.2.8").status_code, 302)
        self.assertFalse(LoginThrottle.objects.filter(subject_key__icontains="viewer").exists())

    def test_profile_bounds_and_ownership(self):
        self.login_viewer()
        self.assertContains(self.profile(self.adult, "delete"), "Нельзя удалить последний")
        self.assertEqual(self.profile(self.foreign, "switch").status_code, 404)
        for index in range(4):
            self.assertEqual(self.client.post(reverse("viewer-profile-create"), {
                "profile_name": f"Имя {index}", "is_kids": "on",
            }).status_code, 302)
        self.assertContains(self.client.post(reverse("viewer-profile-create"), {
            "profile_name": "Шестой",
        }), "не более пяти")
        self.assertEqual(self.user.profiles.count(), 5)

    def test_pin_guard_persists_across_kids_and_locks_across_devices(self):
        self.login_viewer()
        second = Client()
        self.login_viewer(second)
        guarded = self.user.profiles.create(profile_name="Детский", is_kids=True)
        plain = self.user.profiles.create(profile_name="Детский без PIN", is_kids=True)
        self.assertContains(self.profile(guarded, "pin", {"pin": "1234", "current_password": "wrong"}), "Неверный пароль")
        self.profile(guarded, "switch")
        self.assertEqual(self.profile(guarded, "pin", {"pin": "1234", "current_password": PASSWORD}).status_code, 302)
        guarded.refresh_from_db()
        self.assertTrue(check_password("1234", guarded.pin_hash))
        self.profile(guarded, "switch", client=second)
        self.profile(plain, "switch")
        for _ in range(5):
            self.profile(self.adult, "switch", {"pin": "0000"})
        self.assertContains(self.profile(self.adult, "switch", {"pin": "1234"}, client=second), "заблокирована")
        device = Device.objects.get(session_key=self.client.session.session_key)
        self.assertEqual(device.active_profile_id, plain.pk)
        self.assertEqual(device.guarded_profile_id, guarded.pk)
        self.user.refresh_from_db()
        self.assertEqual(self.user.pin_failed_attempts, 5)
        future = timezone.now() + timedelta(minutes=16)
        with patch("accounts.services.timezone.now", return_value=future):
            self.assertEqual(self.profile(self.adult, "switch", {"pin": "1234"}).status_code, 302)
        device.refresh_from_db()
        self.assertFalse(device.kids_locked)

    def test_deleted_guard_needs_owner_password(self):
        self.login_viewer()
        second = Client()
        self.login_viewer(second)
        child = self.user.profiles.create(profile_name="Детский", is_kids=True)
        self.profile(child, "pin", {"pin": "1234", "current_password": PASSWORD})
        self.profile(child, "switch")
        self.assertEqual(self.profile(child, "delete", client=second).status_code, 302)
        self.assertContains(self.profile(self.adult, "switch", {"pin": "1234"}), "Подтвердите текущий пароль")
        self.assertEqual(self.profile(self.adult, "switch", {"current_password": PASSWORD}).status_code, 302)

    def test_kid_catalog_detail_and_media(self):
        self.login_viewer()
        child = self.user.profiles.create(profile_name="Детский", is_kids=True)
        film = Movie.objects.create(title="Для взрослых", is_published=True, published_at=timezone.now(),
                                    age_rating=16, release_year=2020, duration_min=80, description="Фильм")
        detail = reverse("movie-detail", args=[film.pk])
        self.assertContains(self.client.get(detail), film.title)
        with TemporaryDirectory() as directory, override_settings(CATALOG_MEDIA_ROOT=Path(directory)):
            key = f"{uuid4().hex}.mp4"
            (Path(directory) / key).write_bytes(b"data")
            media = MovieMedia.objects.create(movie=film, kind="trailer", size=4, file_format="mp4", storage_key=key)
            endpoint = reverse("movie-media", args=[film.pk, media.pk, media.version_id])
            self.assertEqual(self.client.head(endpoint).status_code, 200)
            self.profile(child, "switch")
            self.assertNotContains(self.client.get(reverse("catalog")), film.title)
            self.assertNotContains(self.client.get(reverse("home")), film.title)
            self.assertEqual(self.client.get(detail).status_code, 404)
            self.assertEqual(self.client.head(endpoint).status_code, 404)

    def test_password_change_requires_password_and_revokes_other_sessions(self):
        self.login_viewer()
        second = Client()
        self.login_viewer(second)
        key = second.session.session_key
        data = {"new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD}
        endpoint = reverse("viewer-password")
        rejected = self.client.post(endpoint, {**data, "old_password": "wrong"})
        self.assertIn("old_password", rejected.context["form"].errors)
        self.assertTrue(Session.objects.filter(session_key=key).exists())
        self.assertRedirects(self.client.post(endpoint, {**data, "old_password": PASSWORD}), reverse("viewer-login"))
        self.assertFalse(Session.objects.filter(session_key=key).exists())

    def test_revoke_selected_device_and_delete_account(self):
        self.login_viewer()
        second, foreign = Client(), Client()
        self.login_viewer(second)
        self.login_viewer(foreign, identifier="another")
        key = second.session.session_key
        selected = Device.objects.get(session_key=key)
        foreign_device = self.other.devices.get()
        self.assertEqual(self.client.post(reverse("viewer-revoke", args=[foreign_device.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("viewer-revoke", args=[selected.pk])).status_code, 405)
        self.assertEqual(self.client.post(reverse("viewer-revoke", args=[selected.pk])).status_code, 302)
        self.assertFalse(Session.objects.filter(session_key=key).exists())
        self.assertRedirects(second.get(reverse("viewer-dashboard")), reverse("viewer-login"))
        self.assertEqual(self.client.get(reverse("viewer-dashboard")).status_code, 200)
        self.login_viewer(second)
        key = second.session.session_key
        entry = AuditEntry.objects.create(actor=self.user, action="test", object_kind="movie", object_ref="1", result_code="ok")
        self.assertContains(self.client.post(reverse("viewer-delete"), {"current_password": "bad"}), "Неверный пароль")
        self.assertTrue(User.objects.filter(pk=self.user.pk).exists())
        self.assertRedirects(self.client.post(reverse("viewer-delete"), {"current_password": PASSWORD}), reverse("home"))
        self.assertFalse(Profile.objects.filter(user_id=self.user.pk).exists())
        self.assertFalse(Device.objects.filter(user_id=self.user.pk).exists())
        self.assertFalse(Session.objects.filter(session_key=key).exists())
        entry.refresh_from_db()
        self.assertIsNone(entry.actor_id)

    def test_reset_single_use_expiry_and_session_revocation(self):
        self.login_viewer()
        key = self.client.session.session_key
        guest = Client()
        reset = reverse("viewer-reset")
        self.assertRedirects(guest.post(reset, {"email": "unknown@example.test"}), reverse("viewer-reset-sent"))
        self.assertEqual(len(mail.outbox), 0)
        self.assertRedirects(guest.post(reset, {"email": self.user.email}), reverse("viewer-reset-sent"))
        self.assertEqual(len(mail.outbox), 1)
        self.user.refresh_from_db()
        token = default_token_generator.make_token(self.user)
        endpoint = reverse("viewer-reset-confirm", args=[urlsafe_base64_encode(force_bytes(self.user.pk)), token])
        future = default_token_generator._now() + timedelta(minutes=31)
        with patch.object(default_token_generator, "_now", return_value=future):
            self.assertContains(guest.get(endpoint), "Ссылка истекла")
        confirmation = guest.get(endpoint)["Location"]
        self.assertRedirects(guest.post(confirmation, {
            "new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD,
        }), reverse("viewer-login"))
        self.assertContains(Client().get(endpoint), "Ссылка истекла")
        self.assertFalse(Session.objects.filter(session_key=key).exists())
        self.assertEqual(self.login_viewer(Client(), password=NEW_PASSWORD).status_code, 302)
