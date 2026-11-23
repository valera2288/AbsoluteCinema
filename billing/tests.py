from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Barrier
from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.db import connections
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from billing import services
from billing.models import Payment, PaymentMethod, PromoCode, Subscription, Tariff
from operations.models import AuditEntry, Notification


def form_token(user, tariff, code="", auto=False):
    return services.checkout_token(user, services.quote(tariff, code, timezone.now()), code, auto)


class BillingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.viewer = User.objects.create_user("billingviewer", "billing@example.test", "Billing8-study!")
        cls.admin = User.objects.create_superuser("billingadmin", "admin-billing@example.test")
        cls.tariff = Tariff.objects.create(name="Вечер", price=Decimal("199.99"), period_days=30, max_streams=2)
        cls.other = Tariff.objects.create(name="Премьера", price=Decimal("399.00"), period_days=60, max_streams=4)
        today = timezone.localdate()
        PromoCode.objects.create(code="FREE", discount_percent=100, valid_from=today, valid_to=today)
        PromoCode.objects.create(code="PAST", discount_percent=20, valid_from=today - timedelta(days=2), valid_to=today - timedelta(days=1))

    def buy(self, code="", auto=False):
        return services.checkout(self.viewer.pk, form_token(self.viewer, self.tariff, code, auto))

    def make_due(self, payment):
        now = timezone.now()
        Subscription.objects.filter(pk=payment.subscription_id).update(start_at=now - timedelta(days=31), end_at=now - timedelta(seconds=1))

    def test_signed_checkout_repeats_switches_and_rejects_stale_confirmation(self):
        original, second = (form_token(self.viewer, self.tariff) for _ in range(2))
        old_choice = form_token(self.viewer, self.other)
        first = services.checkout(self.viewer.pk, original)
        self.assertEqual(services.checkout(self.viewer.pk, second).pk, first.pk)
        with self.assertRaises(ValidationError):
            services.checkout(self.viewer.pk, old_choice)
        replacement = services.checkout(self.viewer.pk, form_token(self.viewer, self.other))
        self.assertEqual(services.confirm_payment(self.viewer.pk, first.operation_id, True).status, "declined")
        self.assertEqual(services.checkout(self.viewer.pk, original).pk, first.pk)
        self.assertIsNone(services.current_subscription(self.viewer.pk))
        services.confirm_payment(self.viewer.pk, replacement.operation_id, True)
        active = services.current_subscription(self.viewer.pk)
        end = active.end_at
        services.confirm_payment(self.viewer.pk, replacement.operation_id, True)
        self.assertEqual(services.current_subscription(self.viewer.pk).end_at, end)
        self.assertEqual(active.max_streams, 4)
        with self.assertRaises(ValidationError):
            self.buy()
        self.assertEqual(Payment.objects.count(), 2)

    def test_decline_and_expired_promo(self):
        with self.assertRaisesMessage(ValidationError, "истёк"):
            self.buy("PAST")
        first = self.buy()
        stale = form_token(self.viewer, self.tariff)
        services.confirm_payment(self.viewer.pk, first.operation_id, False)
        with self.assertRaises(ValidationError):
            services.checkout(self.viewer.pk, stale)
        second = self.buy()
        self.assertEqual(first.subscription_id, second.subscription_id)
        self.assertNotEqual(first.pk, second.pk)
        self.assertFalse(PaymentMethod.objects.exists())

    def test_activation_and_refund_rollback(self):
        payment = self.buy()
        with patch.object(Subscription, "save", side_effect=RuntimeError("simulated failure")):
            with self.assertRaises(RuntimeError):
                services.confirm_payment(self.viewer.pk, payment.operation_id, True)
        payment.refresh_from_db()
        self.assertEqual(payment.status, "pending")
        self.assertFalse(PaymentMethod.objects.exists())
        services.confirm_payment(self.viewer.pk, payment.operation_id, True)
        with patch.object(Subscription, "save", side_effect=RuntimeError("simulated failure")):
            with self.assertRaises(RuntimeError):
                services.refund_payment(self.admin, payment.operation_id)
        payment.refresh_from_db()
        self.assertEqual(payment.status, "succeeded")
        self.assertIsNotNone(services.current_subscription(self.viewer.pk))
        with self.assertRaises(PermissionDenied):
            services.refund_payment(self.viewer, payment.operation_id)
        services.refund_payment(self.admin, payment.operation_id)
        services.refund_payment(self.admin, payment.operation_id)
        self.assertIsNone(services.current_subscription(self.viewer.pk))
        self.assertEqual(AuditEntry.objects.filter(action="payment.refund").count(), 1)
        self.assertEqual(Notification.objects.filter(event_key__startswith="refund:").count(), 1)

    def test_free_period_manual_retry_after_binding_and_removed_method(self):
        payment = self.buy("free", auto=True)
        self.assertEqual((payment.status, payment.amount), ("succeeded", Decimal("0.00")))
        self.assertFalse(PaymentMethod.objects.exists())
        with self.assertRaises(ValidationError):
            services.refund_payment(self.admin, payment.operation_id)
        self.make_due(payment)
        self.assertIsNone(services.current_subscription(self.viewer.pk))
        call_command("process_subscriptions")
        payment.subscription.refresh_from_db()
        self.assertTrue(payment.subscription.renewal_requires_retry)
        services.change_method(self.viewer.pk, True)
        call_command("process_subscriptions")
        payment.subscription.refresh_from_db()
        self.assertTrue(payment.subscription.renewal_requires_retry)
        self.assertEqual((Subscription.objects.count(), Payment.objects.count()), (1, 1))
        attempt = services.renew(self.viewer.pk, payment.subscription_id, manual=True)
        self.assertEqual(services.renew(self.viewer.pk, payment.subscription_id, manual=True).pk, attempt.pk)
        payment.subscription.refresh_from_db()
        self.assertFalse(payment.subscription.renewal_requires_retry)
        services.change_method(self.viewer.pk, False)
        services.change_method(self.viewer.pk, True)
        self.assertEqual(services.confirm_payment(self.viewer.pk, attempt.operation_id, True).status, "declined")
        self.assertEqual(Notification.objects.filter(event_key__startswith="expiry:").count(), 1)
        self.assertEqual(Notification.objects.filter(event_key__startswith="renewal-method:").count(), 1)

    def test_auto_renewal_and_disable_keep_original_period(self):
        payment = self.buy(auto=True)
        services.confirm_payment(self.viewer.pk, payment.operation_id, True)
        self.make_due(payment)
        call_command("process_subscriptions")
        call_command("process_subscriptions")
        active = services.current_subscription(self.viewer.pk)
        self.assertEqual(active.renewal_of_id, payment.subscription_id)
        end = active.end_at
        services.disable_renewal(self.viewer.pk, active.pk)
        active.refresh_from_db()
        self.assertFalse(active.auto_renew)
        self.assertEqual(active.end_at, end)
        self.assertEqual(Subscription.objects.count(), 2)

    def test_late_expiration_and_free_termination(self):
        first = self.buy()
        services.confirm_payment(self.viewer.pk, first.operation_id, True)
        self.make_due(first)
        second = self.buy("free")
        self.assertEqual(second.status, "succeeded")
        services.terminate_free(self.viewer.pk, second.subscription_id)
        second.refresh_from_db()
        self.assertEqual(second.status, "succeeded")
        self.assertIsNone(services.current_subscription(self.viewer.pk))
        self.assertEqual(Notification.objects.filter(event_key__startswith="expiry:").count(), 1)

    def test_viewer_checkout_history_and_account_anonymization(self):
        client = Client()
        self.assertEqual(client.post(reverse("viewer-login"), {
            "username": self.viewer.login, "password": "Billing8-study!",
        }).status_code, 302)
        checkout = reverse("billing-checkout", args=[self.tariff.pk])
        self.assertIn("next=", Client().get(checkout)["Location"])
        self.assertContains(client.get(checkout, {"promo": "PAST"}), "Оформить без промокода")
        quote = client.get(checkout)
        self.assertContains(quote, "Подтвердить оформление")
        pending = client.post(checkout, {"token": quote.context["token"]})
        self.assertEqual(pending.status_code, 302)
        self.assertEqual(client.post(pending["Location"], {"result": "success"}).status_code, 302)
        self.assertContains(client.get(reverse("billing-overview")), "Успешен")
        payment = Payment.objects.get()
        self.assertEqual(client.post(reverse("viewer-delete"), {"current_password": "Billing8-study!"}).status_code, 302)
        payment.refresh_from_db()
        self.assertEqual(payment.status, "succeeded")
        self.assertIsNone(payment.subscription.user_id)
        self.assertEqual(payment.subscription.status, "terminated")
        self.assertFalse(PaymentMethod.objects.exists())


class ConcurrentCheckoutTests(TransactionTestCase):
    def test_identical_forms_share_single_pending_attempt(self):
        viewer = User.objects.create_user("parallel", "parallel@example.test")
        tariff = Tariff.objects.create(name="Параллельный", price=Decimal("100.00"), period_days=1, max_streams=1)
        tokens = [form_token(viewer, tariff) for _ in range(2)]
        gate = Barrier(2)

        def run(token):
            try:
                gate.wait(timeout=5)
                return services.checkout(viewer.pk, token).operation_id
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(run, tokens))
        self.assertEqual(results[0], results[1])
        self.assertEqual(Payment.objects.filter(status="pending").count(), 1)
        self.assertEqual(Subscription.objects.filter(status="pending").count(), 1)
