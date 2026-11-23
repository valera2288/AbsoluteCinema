import csv
from datetime import datetime, timedelta
from io import BytesIO, StringIO
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone
from openpyxl import load_workbook

from accounts.models import User
from accounts.services import delete_account
from billing.models import Payment, Subscription, Tariff
from catalog.models import Movie
from operations.models import AuditEntry, Notification, SupportRequest
from operations.services import create_request, handle_request, send_newsletter, set_marketing
from viewing.models import WatchEvent


class OperationsTests(TestCase):
    password = "Support7-study-example!"

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser("opsadmin", "ops@example.test", cls.password)
        cls.owner = User.objects.create_user("supporter", "support@example.test", cls.password)
        cls.other = User.objects.create_user("outsider", "outside@example.test", cls.password)

    def viewer(self, user):
        client = Client()
        self.assertEqual(client.post(reverse("viewer-login"), {
            "username": user.login, "password": self.password,
        }).status_code, 302)
        return client

    def admin_client(self):
        client = Client()
        client.force_login(self.admin)
        return client

    def test_support_reply_idempotence_owner_and_notification(self):
        viewer, stranger = self.viewer(self.owner), self.viewer(self.other)
        response = viewer.post(reverse("support"), {"subject": "Не работает звук", "body": "Проверяю звук в учебном ролике."})
        self.assertEqual(response.status_code, 302)
        ticket = SupportRequest.objects.get(owner=self.owner)
        self.assertEqual(stranger.get(response["Location"]).status_code, 404)
        admin_client = self.admin_client()
        route = reverse("admin:operations_supportrequest_change", args=[ticket.pk])
        data = {"action": "close", "reply_code": "resolved", "body": "Попробуйте ещё раз.",
                "request_key": admin_client.get(route).context["form"].initial["request_key"]}
        self.assertEqual(admin_client.post(route, data).status_code, 302)
        self.assertEqual(admin_client.post(route, data).status_code, 302)
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, "closed")
        self.assertEqual(ticket.messages.count(), 2)
        self.assertContains(viewer.get(response["Location"]), "Проблема устранена.")
        notification = Notification.objects.get(user=self.owner)
        read = reverse("notification-read", args=[notification.pk])
        self.assertEqual(stranger.post(read).status_code, 404)
        self.assertEqual(viewer.post(read).status_code, 302)
        notification.refresh_from_db()
        self.assertTrue(notification.is_read)

    def test_support_anonymizes_owner_and_staff_without_erasing_phrase(self):
        ticket = create_request(self.owner.pk, "Личная тема", "Личное сообщение.")
        with self.assertRaises(ValidationError):
            handle_request(self.admin.pk, ticket.pk, "close")
        handle_request(self.admin.pk, ticket.pk, "close", reply_code="reviewed", body="Цитата зрителя", request_key=uuid4())
        other = create_request(self.other.pk, "Другая тема", "Другое сообщение.")
        handle_request(self.admin.pk, other.pk, "work", reply_code="retry", body="Текст сотрудника", request_key=uuid4())
        self.assertTrue(delete_account(self.owner.pk, self.password))
        ticket.refresh_from_db()
        self.assertEqual((ticket.owner_id, ticket.subject, ticket.status), (None, "", "closed"))
        self.assertFalse(ticket.messages.exclude(body="", author=None).exists())
        self.assertEqual(ticket.messages.get(author_kind="staff").reply_code, "reviewed")
        self.assertTrue(delete_account(self.admin.pk, self.password))
        reply = other.messages.get(author_kind="staff")
        self.assertEqual((reply.author_id, reply.body, reply.reply_code), (None, "", "retry"))
        self.assertEqual(other.messages.get(author_kind="viewer").body, "Другое сообщение.")

    def test_newsletter_consent_and_replay(self):
        viewer = self.viewer(self.owner)
        self.assertEqual(viewer.post(reverse("notification-inbox"), {"marketing_opt_in": "on"}).status_code, 302)
        set_marketing(self.other.pk, False)
        key = uuid4()
        self.assertEqual(send_newsletter(self.admin, key), 1)
        self.assertEqual(send_newsletter(self.admin, key), 0)
        self.assertFalse(Notification.objects.filter(user=self.other).exists())
        viewer.post(reverse("notification-inbox"), {})
        self.assertEqual(send_newsletter(self.admin, uuid4()), 0)
        self.assertEqual(self.viewer(self.other).get(reverse("admin:operations_newsletter")).status_code, 302)

    def test_reports_count_zero_and_dates_separately_and_escape_formulas(self):
        today = timezone.localdate()
        start = datetime.combine(today, datetime.min.time(), tzinfo=timezone.get_current_timezone())
        tariff = Tariff.objects.create(name="Тариф", price=100, period_days=30, max_streams=1)
        for price, status in ((0, "succeeded"), (100, "refunded")):
            period = Subscription.objects.create(tariff=tariff, user=self.owner, status="terminated",
                tariff_name=tariff.name, base_price=100, discount=100-price, period_days=30, max_streams=1)
            Payment.objects.create(subscription=period, status=status, amount=price, method="simulator",
                paid_at=start, refunded_at=start + timedelta(days=1) if price else None)
        movie = Movie.objects.create(title="=1+2", release_year=2026, duration_min=2, age_rating=0, description="Демо")
        WatchEvent.objects.create(movie=movie, started_at=start - timedelta(seconds=1), completed_at=start)
        route = reverse("admin:operations_reports")
        admin_client = self.admin_client()
        self.assertEqual(admin_client.get(route).status_code, 200)
        params = {"start": today, "end": today}
        self.assertEqual(admin_client.get(route, {**params, "kind": "billing"}).context["rows"][0], (2, 100, 0, 0, 100))
        tomorrow = today + timedelta(days=1)
        refund = admin_client.get(route, {"start": tomorrow, "end": tomorrow, "kind": "billing"})
        self.assertEqual(refund.context["rows"][0], (0, 0, 1, 100, -100))
        csv_response = admin_client.get(route, {**params, "kind": "views", "format": "csv"})
        self.assertEqual(list(csv.reader(StringIO(csv_response.content.decode("utf-8-sig"))))[2][1:], ["'=1+2", "0", "1"])
        xlsx = admin_client.get(route, {**params, "kind": "views", "format": "xlsx"})
        book = load_workbook(BytesIO(xlsx.content))
        self.assertEqual(book.active["B3"].data_type, "s")
        self.assertEqual(book.active["B3"].value, "=1+2")
        self.assertEqual((book.active["C3"].value, book.active["D3"].value), (0, 1))

    def test_backup_permissions_failure_and_role_audit(self):
        admin_client = self.admin_client()
        route = reverse("admin:operations_run_backup")
        self.assertEqual(admin_client.get(route).status_code, 200)
        with patch("operations.backup.backup_root", side_effect=ValueError):
            self.assertEqual(admin_client.post(route).status_code, 302)
        from operations.models import BackupRun
        self.assertEqual(BackupRun.objects.get().status, "failed")
        response = admin_client.post(reverse("admin:accounts_user_change", args=[self.owner.pk]), {
            "login": self.owner.login, "email": self.owner.email, "is_active": "on", "is_staff": "on",
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(AuditEntry.objects.filter(subject_user=self.owner, action="account.roles").exists())
