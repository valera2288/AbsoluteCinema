import re
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
from uuid import UUID, uuid4

from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models.functions import Lower
from django.shortcuts import get_object_or_404
from django.utils import timezone

from accounts.models import User
from accounts.services import locked_user
from billing.models import Payment, PaymentMethod, PromoCode, Subscription, Tariff
from operations.models import AuditEntry, Notification

PERIOD = Subscription.Status
PAY = Payment.Status
FORM_SALT = "billing.checkout.v1"


def current_subscription(user_id, now=None):
    now = now or timezone.now()
    return Subscription.objects.filter(user_id=user_id, status=PERIOD.ACTIVE, start_at__lte=now, end_at__gt=now).first()


def notify(user, event, subject, text):
    Notification.objects.get_or_create(user=user, event_key=event, defaults={"subject": subject, "text": text})


def audit(actor, payment, action):
    AuditEntry.objects.create(actor=actor, action=action, object_kind="payment",
                              object_ref=str(payment.operation_id), result_code=payment.result_code)


def expire_due(user, now):
    from viewing.services import close_sessions

    for period in Subscription.objects.filter(user=user, status=PERIOD.ACTIVE, end_at__lte=now):
        period.status = PERIOD.EXPIRED
        period.save(update_fields=["status"])
        close_sessions("period_expired", subscription=period)
        notify(user, f"expiry:{period.pk}:{period.end_at.isoformat()}", "Подписка истекла", "Оплаченный период завершился.")


def quote(tariff, code, now):
    if not tariff.is_active:
        raise ValidationError("Тариф больше недоступен.")
    promo = None
    if code:
        if not re.fullmatch(r"[A-Za-z0-9-]{3,30}", code):
            raise ValidationError("Введите от 3 до 30 латинских букв, цифр или дефисов.")
        date = timezone.localdate(now)
        promo = PromoCode.objects.alias(normalized=Lower("code")).filter(
            normalized=code.lower(), is_active=True, valid_from__lte=date, valid_to__gte=date,
        ).first()
        if promo is None:
            raise ValidationError("Промокод недействителен или срок его действия истёк.")
    discount = (tariff.price * (promo.discount_percent if promo else 0) / 100).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
    return {"tariff_id": tariff.pk, "tariff_name": tariff.name, "base_price": str(tariff.price),
            "discount": str(discount), "period_days": tariff.period_days, "max_streams": tariff.max_streams,
            "promo_id": promo.pk if promo else None}


def checkout_token(user, terms, code, auto_renew):
    draft = Subscription.objects.filter(user=user, status=PERIOD.PENDING).first()
    latest = draft.payments.order_by("-pk").first() if draft else None
    return signing.dumps({"user": user.pk, "terms": terms, "code": code, "auto": auto_renew,
                          "draft": draft.pk if draft else None, "payment": latest.pk if latest else None,
                          "payment_status": latest.status if latest else None,
                          "operation": str(uuid4())}, salt=FORM_SALT)


def close_draft(draft, code):
    draft.payments.filter(status=PAY.PENDING).update(status=PAY.DECLINED, result_code=code)
    draft.status = PERIOD.TERMINATED
    draft.auto_renew = False
    draft.save(update_fields=["status", "auto_renew"])


def new_attempt(draft, *, operation_id=None, method=None):
    amount = Decimal(draft.base_price) - Decimal(draft.discount)
    return draft.payments.create(amount=amount, method="simulator" if amount else "promo",
                                 operation_id=operation_id or uuid4(), payment_method=method)


@transaction.atomic
def checkout(user_id, signed_token, *, tariff_id=None):
    try:
        data = signing.loads(signed_token, salt=FORM_SALT, max_age=1800)
    except signing.BadSignature as exc:
        raise ValidationError("Форма устарела. Обновите расчёт.") from exc
    if data["user"] != user_id:
        raise PermissionDenied
    if tariff_id is not None and data["terms"]["tariff_id"] != tariff_id:
        raise ValidationError("Форма относится к другому тарифу. Обновите расчёт.")
    user = locked_user(user_id)
    operation_id = UUID(data["operation"])
    repeat = Payment.objects.filter(subscription__user=user, operation_id=operation_id).first()
    if repeat:
        return repeat
    now = timezone.now()
    expire_due(user, now)
    if current_subscription(user.pk, now):
        raise ValidationError("У вас уже есть действующая подписка.")
    draft = Subscription.objects.filter(user=user, status=PERIOD.PENDING).first()
    latest = draft.payments.order_by("-pk").first() if draft else None
    tariff = get_object_or_404(Tariff, pk=data["terms"]["tariff_id"])
    terms = quote(tariff, data["code"], now)
    matches = bool(draft and not draft.renewal_of_id and draft.auto_renew == data["auto"]
                   and all(str(getattr(draft, key)) == str(value) for key, value in terms.items()))
    if terms == data["terms"] and matches and latest and latest.status == PAY.PENDING:
        return latest
    if (terms != data["terms"] or (draft.pk if draft else None) != data["draft"]
            or (latest.pk if latest else None) != data["payment"]
            or (latest.status if latest else None) != data.get("payment_status")):
        raise ValidationError("Условия или попытка изменились. Обновите расчёт.")
    if not matches:
        if draft:
            close_draft(draft, "replaced")
        draft = Subscription.objects.create(user=user, auto_renew=data["auto"], **terms)
    payment = new_attempt(draft, operation_id=operation_id)
    return settle(user, payment, True, now) if not payment.amount else payment


def settle(user, payment, success, now):
    draft = payment.subscription
    if payment.status != PAY.PENDING or draft.status != PERIOD.PENDING:
        return payment
    expire_due(user, now)
    if current_subscription(user.pk, now):
        raise ValidationError("У вас уже есть действующая подписка.")
    if draft.renewal_of_id and (not draft.renewal_of.auto_renew or not PaymentMethod.objects.filter(
        pk=payment.payment_method_id, user=user,
    ).exists()):
        success = False
    payment.status = PAY.SUCCEEDED if success else PAY.DECLINED
    payment.result_code = "approved" if success else "declined"
    payment.paid_at = now if success else None
    payment.save(update_fields=["status", "result_code", "paid_at"])
    if success:
        if payment.amount:
            PaymentMethod.objects.get_or_create(user=user)
        draft.status = PERIOD.ACTIVE
        draft.start_at = now
        draft.end_at = now + timedelta(days=draft.period_days)
        draft.save(update_fields=["status", "start_at", "end_at"])
    audit(user, payment, "payment.confirm")
    notify(user, f"payment:{payment.operation_id}", "Результат оплаты",
           "Подписка активна." if success else "Тестовый платёж отклонён.")
    return payment


@transaction.atomic
def confirm_payment(user_id, operation_id, success):
    user = locked_user(user_id)
    payment = get_object_or_404(Payment.objects.select_related("subscription"), operation_id=operation_id, subscription__user=user)
    return settle(user, payment, success, timezone.now())


@transaction.atomic
def refund_payment(actor, operation_id):
    if not actor.is_active or not actor.is_staff or not actor.has_perm("billing.refund_payment"):
        raise PermissionDenied
    candidate = get_object_or_404(Payment.objects.select_related("subscription"), operation_id=operation_id)
    owner = get_object_or_404(User.objects.select_for_update(), pk=candidate.subscription.user_id)
    payment = get_object_or_404(Payment.objects.select_related("subscription"), pk=candidate.pk, subscription__user=owner)
    if payment.status == PAY.REFUNDED:
        return payment
    now = timezone.now()
    active = current_subscription(owner.pk, now)
    if not payment.amount:
        raise ValidationError("Для бесплатного периода используйте прекращение подписки, а не денежный возврат.")
    if payment.status != PAY.SUCCEEDED or not active or active.pk != payment.subscription_id:
        raise ValidationError("Возврат доступен только для успешной оплаты текущего периода.")
    payment.status, payment.refunded_at, payment.result_code = PAY.REFUNDED, now, "refunded"
    payment.save(update_fields=["status", "refunded_at", "result_code"])
    end_period(active, now)
    audit(actor, payment, "payment.refund")
    notify(owner, f"refund:{payment.operation_id}", "Платёж возвращён", "Доступ по подписке прекращён.")
    return payment


def end_period(period, now):
    from viewing.services import close_sessions

    period.status, period.ended_at, period.auto_renew = PERIOD.TERMINATED, now, False
    period.save(update_fields=["status", "ended_at", "auto_renew"])
    close_sessions("period_ended", subscription=period)


@transaction.atomic
def terminate_free(owner_id, period_id, actor=None):
    if actor is not None and (not actor.is_active or not actor.is_staff or not actor.has_perm("billing.refund_payment")):
        raise PermissionDenied
    owner = get_object_or_404(User.objects.select_for_update(), pk=owner_id)
    period = get_object_or_404(Subscription, pk=period_id, user=owner)
    if period.status == PERIOD.TERMINATED and period.payments.filter(status=PAY.SUCCEEDED, amount=0).exists():
        return
    now = timezone.now()
    if period != current_subscription(owner.pk, now) or not period.payments.filter(status=PAY.SUCCEEDED, amount=0).exists():
        raise ValidationError("Прекратить можно только текущий бесплатный период.")
    end_period(period, now)


def latest_paid(user):
    return Subscription.objects.filter(user=user, start_at__isnull=False).order_by("-start_at", "-pk").first()


@transaction.atomic
def disable_renewal(user_id, period_id):
    user = locked_user(user_id)
    period = get_object_or_404(Subscription, pk=period_id, user=user, start_at__isnull=False)
    period.auto_renew = False
    period.save(update_fields=["auto_renew"])
    draft = Subscription.objects.filter(user=user, status=PERIOD.PENDING, renewal_of=period).first()
    if draft:
        close_draft(draft, "renewal_disabled")


@transaction.atomic
def change_method(user_id, bind):
    user = locked_user(user_id)
    if bind:
        PaymentMethod.objects.get_or_create(user=user)
    else:
        method = PaymentMethod.objects.filter(user=user).first()
        if method:
            Payment.objects.filter(payment_method=method, status=PAY.PENDING).update(status=PAY.DECLINED, result_code="method_removed")
            method.delete()


@transaction.atomic
def renew(user_id, period_id=None, *, manual=False):
    user = User.objects.select_for_update().filter(pk=user_id, is_active=True).first()
    if not user:
        return None
    now = timezone.now()
    expire_due(user, now)
    period = latest_paid(user)
    if period_id and (not period or period.pk != period_id):
        existing = Payment.objects.filter(subscription__renewal_of_id=period_id, subscription__user=user).order_by("-pk").first()
        return existing if existing and existing.status == PAY.SUCCEEDED else None
    if not period or period.status != PERIOD.EXPIRED or not period.auto_renew or (period.renewal_requires_retry and not manual):
        return None
    draft = Subscription.objects.filter(user=user, status=PERIOD.PENDING).first()
    if draft and draft.renewal_of_id != period.pk:
        return None
    tariff = period.tariff
    if not tariff.is_active:
        return None
    method = PaymentMethod.objects.filter(user=user).first()
    if not method:
        period.renewal_requires_retry = True
        period.save(update_fields=["renewal_requires_retry"])
        notify(user, f"renewal-method:{period.pk}", "Автопродление не выполнено", "Привяжите тестовый способ оплаты и повторите продление вручную.")
        return None
    if not draft:
        if Subscription.objects.filter(renewal_of=period).exists():
            return None
        draft = Subscription.objects.create(user=user, renewal_of=period, auto_renew=True, **quote(tariff, "", now))
    latest = draft.payments.order_by("-pk").first()
    if latest and latest.status == PAY.PENDING:
        payment = latest
    elif latest and not manual:
        return None
    else:
        payment = new_attempt(draft, method=method)
    if manual and period.renewal_requires_retry:
        period.renewal_requires_retry = False
        period.save(update_fields=["renewal_requires_retry"])
    return payment if manual else settle(user, payment, True, now)


def detach_billing(user):
    for period in Subscription.objects.filter(user=user, status__in=[PERIOD.PENDING, PERIOD.ACTIVE]):
        if period.status == PERIOD.PENDING:
            close_draft(period, "account_removed")
        else:
            end_period(period, timezone.now())
