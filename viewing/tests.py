from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from uuid import uuid4

from django.conf import settings
from django.db import connections
from django.test import Client, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import Device, User
from accounts.services import revoke_device
from billing import services as billing
from billing.models import Payment, Tariff
from catalog.models import Movie, MovieMedia
from viewing import services
from viewing.models import PlaybackSession, WatchEvent, WatchHistory

PASSWORD = "Viewing8-study!"


class ViewingTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user("watchviewer", "watch@example.test", PASSWORD)
        self.admin = User.objects.create_superuser("watchadmin", "watchadmin@example.test")
        self.tariff = Tariff.objects.create(name="Один экран", price=100, period_days=30, max_streams=1)
        self.movie = Movie.objects.create(title="Проверочный фильм", age_rating=16, release_year=2024,
                                         duration_min=2, description="Демо", is_published=True, published_at=timezone.now())
        self.client = self.login()
        self.device = self.user.devices.get()
        self.auth = (self.user.pk, self.device.session_key)
        self.tab = uuid4()
        self.folder = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(override_settings(CATALOG_MEDIA_ROOT=self.folder))
        self.bytes = (settings.BASE_DIR / "private" / "online-cinema-probe.mp4").read_bytes()
        key = f"{uuid4().hex}.mp4"
        (self.folder / key).write_bytes(self.bytes)
        self.video = MovieMedia.objects.create(movie=self.movie, kind="video", size=len(self.bytes),
                                               storage_key=key, file_format="mp4", duration=80, allow_download=True)
        terms = billing.quote(Tariff.objects.get(pk=self.tariff.pk), "", timezone.now())
        payment = billing.checkout(self.user.pk, billing.checkout_token(self.user, terms, "", False))
        self.payment = billing.confirm_payment(self.user.pk, payment.operation_id, True)

    def login(self):
        client = Client()
        response = client.post(reverse("viewer-login"), {"username": self.user.login, "password": PASSWORD})
        self.assertEqual(response.status_code, 302)
        return client

    def token(self, *, device=None, tab=None, replace=None):
        return services.make_intent(device or self.device, self.movie.pk, uuid4(), tab or self.tab, replace)

    def start(self, token=None):
        return services.start_playback(*self.auth, token or self.token())

    def position(self, session, seq, value):
        return services.activity(*self.auth, session.pk, sequence=seq, position=value, media_id=self.video.pk)

    def media(self, session, client=None):
        client = self.client if client is None else client
        return client.get(reverse("watch-media", args=[session.pk, self.video.pk, self.video.version_id]),
                          HTTP_RANGE="bytes=4000000-4000063")

    def test_parallel_limit_timeout_restart_and_completion(self):
        gate = Barrier(2)
        tokens = [self.token(tab=uuid4()), self.token(tab=uuid4())]

        def run(token):
            try:
                gate.wait(timeout=10)
                return self.start(token)
            except services.PlaybackError as exc:
                return exc.code
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, tokens))
        self.assertIn("limit", results)
        first_index = 1 if results[0] == "limit" else 0
        first = results[first_index]
        self.assertEqual(self.start(tokens[first_index]).pk, first.pk)
        self.assertEqual(WatchEvent.objects.count(), 1)
        self.position(first, 1, 24)
        PlaybackSession.objects.filter(pk=first.pk).update(last_seen_at=timezone.now() - services.IDLE_LIMIT)
        self.assertEqual(self.media(first).status_code, 403)
        second = self.start()
        self.assertEqual(WatchHistory.objects.get().progress_sec, 24)
        self.position(second, 1, 76)
        stamp = WatchEvent.objects.get(session=second).completed_at
        self.assertIsNotNone(stamp)
        self.position(second, 2, 2)
        self.position(second, 2, 60)
        self.assertEqual(WatchHistory.objects.get().progress_sec, 2)
        self.position(second, 3, 76)
        self.assertEqual(WatchEvent.objects.get(session=second).completed_at, stamp)
        self.assertEqual(WatchEvent.objects.filter(completed_at__isnull=False).count(), 1)
        with self.assertRaisesMessage(services.PlaybackError, "другом плеере"):
            self.start(self.token(tab=uuid4(), replace=second.pk))
        third = self.start(self.token(replace=second.pk))
        history = WatchHistory.objects.get()
        self.assertEqual((history.progress_sec, history.is_completed), (0, False))
        self.assertEqual(services.live_sessions(timezone.now()).count(), 1)
        with self.assertRaises(services.PlaybackError):
            self.position(second, 4, 78)
        self.position(third, 1, 76)
        self.assertEqual(WatchEvent.objects.count(), 3)
        self.assertEqual(WatchEvent.objects.filter(completed_at__isnull=False).count(), 2)

    def test_endpoint_range_version_refresh_download_and_revocation(self):
        intent = self.client.post(reverse("watch-intent", args=[self.movie.pk]), {"id": uuid4(), "tab": self.tab})
        result = self.client.post(reverse("watch-start"), {"token": intent.json()["token"]})
        self.assertEqual(result.status_code, 200)
        session = PlaybackSession.objects.get(pk=result.json()["id"])
        response = self.media(session)
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response["Content-Range"], f"bytes 4000000-4000063/{len(self.bytes)}")
        self.assertEqual(b"".join(response.streaming_content), self.bytes[4000000:4000064])
        endpoint = reverse("watch-media", args=[session.pk, self.video.pk, self.video.version_id])
        self.assertEqual(self.client.get(endpoint, HTTP_RANGE="bytes=0-8,10-20").status_code, 400)
        self.assertEqual(self.client.get(endpoint, HTTP_RANGE=f"bytes={len(self.bytes)}-").status_code, 416)
        self.position(session, 1, 24)
        download = self.client.head(reverse("watch-download", args=[self.movie.pk, self.video.pk]))
        self.assertEqual(download.status_code, 200)
        self.assertIn("attachment", download["Content-Disposition"])
        download.close()
        self.assertEqual(self.client.get(reverse("movie-media", args=[self.movie.pk, self.video.pk, self.video.version_id])).status_code, 404)
        player = self.client.get(reverse("watch-player", args=[self.movie.pk]))
        self.assertEqual(player.status_code, 200)
        self.device.refresh_from_db()
        self.assertContains(player, f'data-device="{self.device.pk}"')
        self.assertContains(player, f'data-profile="{self.device.active_profile_id}"')
        MovieMedia.objects.filter(pk=self.video.pk).update(version_id=uuid4())
        stale = self.media(session)
        self.assertEqual((stale.status_code, stale.content), (410, b""))
        self.video.refresh_from_db()
        refresh = self.client.post(reverse("watch-refresh", args=[session.pk])).json()
        self.assertEqual((refresh["id"], refresh["position"]), (str(session.pk), 24))
        self.assertIn(str(self.video.version_id), refresh["video"][0]["url"])
        self.assertEqual(WatchEvent.objects.count(), 1)
        second = self.login()
        other = self.user.devices.exclude(pk=self.device.pk).get()
        self.assertEqual(self.media(session, second).status_code, 403)
        self.assertEqual(second.post(reverse("viewer-revoke", args=[self.device.pk])).status_code, 302)
        session.refresh_from_db()
        self.assertEqual(session.close_reason, "device_revoked")
        self.assertEqual(self.media(session).status_code, 403)
        replacement = services.start_playback(self.user.pk, other.session_key, self.token(device=other))
        self.assertIsNotNone(replacement.pk)
        billing.refund_payment(self.admin, self.payment.operation_id)
        self.assertEqual(self.media(replacement, second).status_code, 403)
        replacement.refresh_from_db()
        self.assertEqual(replacement.close_reason, "period_ended")

    def test_profile_isolation_blocking_and_anonymization(self):
        first = self.start()
        self.position(first, 1, 20)
        other = self.user.profiles.create(profile_name="Другой")
        self.assertEqual(self.client.post(reverse("viewer-profile", args=[other.pk, "switch"])).status_code, 302)
        self.device.refresh_from_db()
        self.assertEqual(self.media(first).status_code, 403)
        second = self.start()
        self.position(second, 1, 40)
        self.assertEqual(set(WatchHistory.objects.values_list("progress_sec", flat=True)), {20, 40})
        self.assertContains(self.client.get(reverse("viewer-dashboard")), "40 сек.")
        self.assertNotContains(self.client.get(reverse("viewer-dashboard")), "20 сек.")
        admin_client = Client()
        admin_client.force_login(self.admin)
        self.assertEqual(admin_client.post(reverse("admin:accounts_user_change", args=[self.user.pk]), {
            "login": self.user.login, "email": self.user.email,
        }).status_code, 302)
        second.refresh_from_db()
        self.assertEqual(second.close_reason, "device_revoked")
        self.assertEqual(self.media(second).status_code, 403)
        User.objects.filter(pk=self.user.pk).update(is_active=True)
        self.client = self.login()
        self.assertEqual(self.client.post(reverse("viewer-delete"), {"current_password": PASSWORD}).status_code, 302)
        self.assertFalse(WatchHistory.objects.exists())
        self.assertFalse(PlaybackSession.objects.exists())
        self.assertEqual(WatchEvent.objects.filter(session__isnull=True, profile__isnull=True).count(), 2)
        payment = Payment.objects.get(pk=self.payment.pk)
        self.assertEqual(payment.subscription.status, "terminated")
        self.assertIsNone(payment.subscription.user_id)

    def test_guards_and_cleanup(self):
        child = self.user.profiles.create(profile_name="Детский", is_kids=True)
        self.client.post(reverse("viewer-profile", args=[child.pk, "switch"]))
        self.device.refresh_from_db()
        with self.assertRaisesMessage(services.PlaybackError, "профиля"):
            self.start()
        self.assertEqual(self.client.head(reverse("watch-download", args=[self.movie.pk, self.video.pk])).status_code, 403)
        adult = self.user.profiles.filter(is_kids=False).get()
        self.client.post(reverse("viewer-profile", args=[adult.pk, "switch"]))
        self.device.refresh_from_db()
        session = self.start()
        self.position(session, 1, 24)
        with self.assertRaisesMessage(services.PlaybackError, "длительности"):
            self.position(session, 2, 100)
        PlaybackSession.objects.filter(pk=session.pk).update(closed_at=timezone.now() - timedelta(days=2))
        self.assertEqual(services.cleanup_sessions(), 1)
        self.assertEqual(WatchHistory.objects.get().progress_sec, 24)
        self.assertIsNone(WatchHistory.objects.get().writer_session_id)
        self.assertIsNone(WatchEvent.objects.get().session_id)
