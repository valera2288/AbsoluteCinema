from decimal import Decimal
from uuid import uuid4

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models
from django.db.models import F, Q
from django.db.models.functions import Lower


class Tariff(models.Model):
    name = models.CharField("название", max_length=100, unique=True)
    price = models.DecimalField("цена, ₽", max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])
    period_days = models.PositiveIntegerField("срок, дней", validators=[MinValueValidator(1)])
    description = models.TextField("описание", blank=True)
    max_streams = models.PositiveIntegerField("лимит просмотров", validators=[MinValueValidator(1)])
    is_active = models.BooleanField("доступен", default=True)

    class Meta:
        db_table = "tariffs"
        ordering = ["price", "pk"]
        verbose_name = "тариф"
        verbose_name_plural = "тарифы"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(price__gt=0, period_days__gt=0, max_streams__gt=0),
                name="tariff_positive_terms",
            ),
        ]

    def __str__(self):
        return self.name


class PromoCode(models.Model):
    code = models.CharField("код", max_length=30, validators=[RegexValidator(r"\A[A-Za-z0-9-]{3,30}\Z")])
    discount_percent = models.PositiveSmallIntegerField(validators=[MinValueValidator(1), MaxValueValidator(100)])
    valid_from = models.DateField()
    valid_to = models.DateField()
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "promo_codes"
        verbose_name_plural = "промокоды"
        constraints = [
            models.UniqueConstraint(Lower("code"), name="promo_unique_lower_code"),
            models.CheckConstraint(condition=Q(discount_percent__range=(1, 100)), name="promo_discount_range"),
            models.CheckConstraint(condition=Q(valid_from__lte=F("valid_to")), name="promo_date_order"),
        ]

    def __str__(self):
        return self.code


class Subscription(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Ожидает оплаты"
        ACTIVE = "active", "Активна"
        EXPIRED = "expired", "Истекла"
        TERMINATED = "terminated", "Прекращена"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    tariff = models.ForeignKey(Tariff, on_delete=models.PROTECT)
    promo = models.ForeignKey(PromoCode, null=True, on_delete=models.PROTECT)
    renewal_of = models.OneToOneField("self", null=True, on_delete=models.PROTECT)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    tariff_name = models.CharField(max_length=100)
    base_price = models.DecimalField(max_digits=10, decimal_places=2)
    discount = models.DecimalField(max_digits=10, decimal_places=2)
    period_days = models.PositiveIntegerField()
    max_streams = models.PositiveIntegerField()
    auto_renew = models.BooleanField(default=False)
    renewal_requires_retry = models.BooleanField(default=False)
    start_at = models.DateTimeField(null=True)
    end_at = models.DateTimeField(null=True)
    ended_at = models.DateTimeField(null=True)

    class Meta:
        db_table = "subscriptions"
        verbose_name_plural = "подписки"
        constraints = [
            models.UniqueConstraint(fields=["user"], condition=Q(status="pending"), name="one_pending_subscription"),
            models.UniqueConstraint(fields=["user"], condition=Q(status="active"), name="one_active_subscription"),
            models.CheckConstraint(condition=Q(status__in=["pending", "active", "expired", "terminated"]), name="subscription_known_status"),
            models.CheckConstraint(condition=~Q(status__in=["pending", "active"]) | Q(user__isnull=False), name="subscription_live_owner"),
            models.CheckConstraint(condition=Q(start_at__isnull=True, end_at__isnull=True) | Q(start_at__isnull=False, end_at__isnull=False, start_at__lt=F("end_at")), name="subscription_dates"),
            models.CheckConstraint(condition=~Q(status__in=["active", "expired"]) | Q(end_at__isnull=False), name="subscription_paid_dates"),
            models.CheckConstraint(condition=Q(base_price__gt=0, discount__gte=0, discount__lte=F("base_price"), period_days__gt=0, max_streams__gt=0), name="subscription_terms"),
        ]
        indexes = [models.Index(fields=["status", "end_at"], name="subscription_due"), models.Index(fields=["user", "-id"], name="subscription_history")]


class PaymentMethod(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    test_token = models.UUIDField(default=uuid4, unique=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def masked(self):
        return f"•••• {self.test_token.hex[-4:]}"

    class Meta:
        db_table = "payment_methods"


class Payment(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Ожидает подтверждения"
        SUCCEEDED = "succeeded", "Успешен"
        DECLINED = "declined", "Отклонён"
        REFUNDED = "refunded", "Возвращён"

    subscription = models.ForeignKey(Subscription, on_delete=models.PROTECT, related_name="payments")
    payment_method = models.ForeignKey(PaymentMethod, null=True, on_delete=models.SET_NULL)
    operation_id = models.UUIDField(default=uuid4, unique=True, editable=False)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    method = models.CharField(max_length=12, choices=[("simulator", "Имитатор"), ("promo", "Промокод")])
    result_code = models.CharField(max_length=32, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    paid_at = models.DateTimeField(null=True)
    refunded_at = models.DateTimeField(null=True)

    class Meta:
        db_table = "payments"
        verbose_name_plural = "платежи"
        permissions = [("refund_payment", "Может возвращать учебные платежи")]
        constraints = [
            models.UniqueConstraint(fields=["subscription"], condition=Q(status="pending"), name="one_pending_payment"),
            models.UniqueConstraint(fields=["subscription"], condition=Q(status__in=["succeeded", "refunded"]), name="one_settled_payment"),
            models.CheckConstraint(condition=Q(amount__gte=0), name="payment_nonnegative"),
            models.CheckConstraint(condition=Q(status__in=["pending", "declined"], paid_at__isnull=True, refunded_at__isnull=True)
                                   | Q(status="succeeded", paid_at__isnull=False, refunded_at__isnull=True)
                                   | Q(status="refunded", amount__gt=0, paid_at__isnull=False, refunded_at__isnull=False,
                                       refunded_at__gte=F("paid_at")), name="payment_status_dates"),
        ]
        indexes = [
            models.Index(fields=["subscription", "-created_at"], name="payment_history"),
            models.Index(fields=["paid_at"], condition=Q(paid_at__isnull=False), name="payment_paid_date"),
            models.Index(fields=["refunded_at"], condition=Q(refunded_at__isnull=False), name="payment_refund_date"),
        ]
