from decimal import Decimal

from django import forms
from django.contrib import messages
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST, require_safe

from accounts.views import viewer_required
from billing import services
from billing.models import Payment, PaymentMethod, Subscription, Tariff
from operations.models import Notification


class QuoteForm(forms.Form):
    promo = forms.CharField(label="Промокод", max_length=30, required=False)
    auto_renew = forms.BooleanField(label="Включить учебное автопродление", required=False)


@viewer_required
@require_safe
def overview(request):
    return render(request, "billing/overview.html", {
        "active": services.current_subscription(request.user.pk),
        "latest": services.latest_paid(request.user),
        "draft": Payment.objects.filter(subscription__user=request.user, subscription__status="pending",
                                        status="pending").first(),
        "payments": Payment.objects.filter(subscription__user=request.user).select_related("subscription").order_by("-pk")[:50],
        "method": PaymentMethod.objects.filter(user=request.user).first(),
        "notifications": Notification.objects.filter(user=request.user).order_by("-sent_at", "-pk")[:10],
    })


@never_cache
@require_http_methods(["GET", "POST"])
def checkout(request, tariff_id):
    if not request.viewer_device:
        return redirect_to_login(request.get_full_path())
    tariff = get_object_or_404(Tariff, pk=tariff_id)
    form = QuoteForm(request.GET)
    terms = token = amount = None
    if request.method == "POST":
        try:
            payment = services.checkout(request.user.pk, request.POST.get("token", ""), tariff_id=tariff.pk)
        except ValidationError as exc:
            messages.error(request, exc.messages[0])
        else:
            return redirect("billing-payment", operation_id=payment.operation_id)
    if form.is_valid():
        try:
            terms = services.quote(tariff, form.cleaned_data["promo"], timezone.now())
        except ValidationError as exc:
            form.add_error("promo" if tariff.is_active else None, exc)
        else:
            amount = Decimal(terms["base_price"]) - Decimal(terms["discount"])
            token = services.checkout_token(request.user, terms, form.cleaned_data["promo"], form.cleaned_data["auto_renew"])
    return render(request, "billing/checkout.html", {
        "form": form, "tariff": tariff, "terms": terms, "token": token, "amount": amount,
        "without_promo": services.checkout_token(
            request.user, services.quote(tariff, "", timezone.now()), "", request.GET.get("auto_renew") == "on",
        ) if tariff.is_active and "promo" in form.errors else None,
    })


@viewer_required
@require_http_methods(["GET", "POST"])
def payment_result(request, operation_id):
    payment = get_object_or_404(Payment.objects.select_related("subscription"), operation_id=operation_id,
                               subscription__user=request.user)
    if request.method == "POST" and request.POST.get("result") in ("success", "decline"):
        try:
            services.confirm_payment(request.user.pk, operation_id, request.POST["result"] == "success")
        except ValidationError as exc:
            messages.error(request, exc.messages[0])
        return redirect("billing-payment", operation_id=operation_id)
    return render(request, "billing/payment.html", {"payment": payment})


@viewer_required
@require_POST
def method_action(request):
    if request.POST.get("action") in ("bind", "remove"):
        services.change_method(request.user.pk, bind=request.POST["action"] == "bind")
    return redirect("billing-overview")


@viewer_required
@require_POST
def period_action(request, period_id):
    get_object_or_404(Subscription, pk=period_id, user=request.user)
    action = request.POST.get("action")
    try:
        if action == "disable":
            services.disable_renewal(request.user.pk, period_id)
        elif action == "terminate":
            services.terminate_free(request.user.pk, period_id)
        elif action == "retry":
            payment = services.renew(request.user.pk, period_id, manual=True)
            if payment:
                return redirect("billing-payment", operation_id=payment.operation_id)
            messages.error(request, "Продление недоступно. Проверьте тариф, способ оплаты и текущие попытки.")
    except ValidationError as exc:
        messages.error(request, exc.messages[0])
    return redirect("billing-overview")
