from pathlib import Path
from tempfile import TemporaryDirectory

from django.contrib.auth.models import Group
from django.contrib.sessions.models import Session
from django.contrib.staticfiles import finders
from django.contrib.staticfiles.handlers import StaticFilesHandler
from django.core.management import call_command
from django.http import Http404
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from accounts.models import User
from config.media_probe import MIB
from config.probe_urls import urlpatterns as probe_patterns
from config.urls import urlpatterns as site_patterns

PROBE_URL = "/probe/video/"
PATTERN = bytes(range(256))
urlpatterns = [*site_patterns, *probe_patterns]


class DefaultRoutesTests(TestCase):
    def test_home_and_probe_disabled(self):
        home = self.client.get("/")
        self.assertContains(home, reverse("viewer-login"))
        self.assertNotContains(home, reverse("admin:index"))
        self.assertIsNotNone(finders.find("css/site.css"))
        self.assertEqual(self.client.get("/probe/").status_code, 404)
        self.assertEqual(self.client.get(PROBE_URL).status_code, 404)


@override_settings(ROOT_URLCONF=__name__)
class MediaProbeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("setup_roles")
        cls.viewer = User.objects.create_user("viewer", "viewer@example.test")
        cls.probe_group = Group.objects.get(name="Пробный просмотр")
        cls.viewer.groups.add(cls.probe_group)

    def setUp(self):
        workdir = Path(self.enterContext(TemporaryDirectory()))
        self.probe_file = workdir / "sample.mp4"
        self.payload = PATTERN * (5 * MIB // len(PATTERN))
        self.probe_file.write_bytes(self.payload)
        self.enterContext(override_settings(MEDIA_PROBE_FILE=str(self.probe_file)))

    def test_requires_permission_on_each_request_and_never_serves_direct_path(self):
        self.assertEqual(self.client.get("/probe/").status_code, 403)
        self.assertEqual(self.client.get(PROBE_URL, HTTP_RANGE="bytes=0-8").status_code, 403)
        self.client.force_login(self.viewer)
        self.assertContains(self.client.get("/probe/"), PROBE_URL)
        response = self.client.get(PROBE_URL, HTTP_RANGE="bytes=100-105")
        self.assertEqual(response.status_code, 206)
        self.assertEqual(b"".join(response.streaming_content), self.payload[100:106])
        self.viewer.groups.remove(self.probe_group)
        denied = self.client.get(PROBE_URL, HTTP_RANGE="bytes=4000000-4000008")
        self.assertEqual(denied.status_code, 403)
        self.assertNotIn("Content-Range", denied.headers)
        self.assertIn("no-store", denied.headers["Cache-Control"])
        self.assertEqual(self.client.head(PROBE_URL).status_code, 403)
        self.assertEqual(self.client.get("/sample.mp4").status_code, 404)
        with self.assertRaises(Http404):
            StaticFilesHandler(None).serve(RequestFactory().get("/static/sample.mp4"))

    def test_head_and_single_ranges_return_exact_lengths_and_data(self):
        self.client.force_login(self.viewer)
        size = len(self.payload)
        head = self.client.head(PROBE_URL)
        self.assertEqual(head.status_code, 200)
        self.assertEqual(head.headers["Content-Length"], str(size))
        self.assertEqual(head.content, b"")
        for header, start, end in (
            ("bytes=0-8", 0, 8),
            (f"bytes={size - 9}-", size - 9, size - 1),
            ("bytes=-9", size - 9, size - 1),
            (f"bytes={size - 9}-999999999999999999", size - 9, size - 1),
            ("bytes=0-", 0, size - 1),
        ):
            with self.subTest(header=header):
                response = self.client.get(PROBE_URL, HTTP_RANGE=header)
                self.assertEqual(response.status_code, 206)
                self.assertEqual(response.headers["Accept-Ranges"], "bytes")
                self.assertEqual(response.headers["Content-Type"], "video/mp4")
                self.assertEqual(response.headers["Content-Range"], f"bytes {start}-{end}/{size}")
                self.assertEqual(response.headers["Content-Length"], str(end - start + 1))
                self.assertIn("no-store", response.headers["Cache-Control"])
                self.assertEqual(b"".join(response.streaming_content), self.payload[start:end + 1])

    def test_invalid_or_unsatisfiable_ranges_and_missing_file(self):
        self.client.force_login(self.viewer)
        for header in ("bytes=-", "bytes=one-two", "bytes=0-1,4-5", "items=0-1"):
            with self.subTest(header=header):
                self.assertEqual(self.client.get(PROBE_URL, HTTP_RANGE=header).status_code, 400)
        for header in ("bytes=-0", "bytes=9-3", f"bytes={len(self.payload)}-"):
            with self.subTest(header=header):
                response = self.client.get(PROBE_URL, HTTP_RANGE=header)
                self.assertEqual(response.status_code, 416)
                self.assertEqual(response.headers["Content-Range"], f"bytes */{len(self.payload)}")
        with override_settings(MEDIA_PROBE_FILE=""):
            self.assertEqual(self.client.get(PROBE_URL).status_code, 404)
        with override_settings(STATIC_ROOT=self.probe_file.parent):
            self.assertEqual(self.client.get(PROBE_URL).status_code, 404)

    def test_stream_stops_after_session_is_removed(self):
        self.client.force_login(self.viewer)
        response = self.client.get(PROBE_URL)
        self.assertEqual(response.status_code, 200)
        chunks = iter(response.streaming_content)
        self.assertEqual(next(chunks), self.payload[:MIB])
        Session.objects.filter(session_key=self.client.session.session_key).delete()
        self.assertEqual(list(chunks), [])
        self.assertEqual(self.client.get(PROBE_URL, HTTP_RANGE="bytes=0-8").status_code, 403)
