from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path, reverse
from django.utils.html import format_html

from billing.models import Payment, PromoCode, Subscription, Tariff
from billing.services import refund_payment, terminate_free


@admin.register(Tariff)
class TariffAdmin(admin.ModelAdmin):
    list_display = ("name", "price", "period_days", "max_streams", "is_active")
    list_filter = ("is_active",)


@admin.register(PromoCode)
class PromoAdmin(admin.ModelAdmin):
    list_display = ("code", "discount_percent", "valid_from", "valid_to", "is_active")


class HistoryAdmin(admin.ModelAdmin):
    actions = None

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Subscription)
class SubscriptionAdmin(HistoryAdmin):
    list_display = ("id", "user", "tariff_name", "status", "end_at", "auto_renew")
    list_filter = ("status", "auto_renew")
    list_select_related = ("user",)


@admin.register(Payment)
class PaymentAdmin(HistoryAdmin):
    exclude = ("payment_method",)
    readonly_fields = ("operation_id",)
    list_display = ("operation_id", "subscription_id", "amount", "status", "created_at", "settlement_link")
    list_filter = ("status",)

    @admin.display(description="Действие")
    def settlement_link(self, payment):
        if payment.status == Payment.Status.SUCCEEDED:
            return format_html('<a href="{}">{}</a>', reverse("admin:billing_settlement", args=[payment.operation_id]),
                               "Вернуть платёж" if payment.amount else "Прекратить бесплатный период")
        return "—"

    def get_urls(self):
        return [path("settlement/<uuid:operation_id>/", self.admin_site.admin_view(self.settlement),
                     name="billing_settlement")] + super().get_urls()

    def settlement(self, request, operation_id):
        if not request.user.has_perm("billing.refund_payment"):
            raise PermissionDenied
        payment = get_object_or_404(Payment.objects.select_related("subscription"), operation_id=operation_id)
        if request.method == "POST":
            try:
                if payment.amount:
                    refund_payment(request.user, operation_id)
                else:
                    terminate_free(payment.subscription.user_id, payment.subscription_id, request.user)
            except ValidationError as exc:
                messages.error(request, exc.messages[0])
            else:
                messages.success(request, "Операция завершена.")
            return redirect("admin:billing_payment_changelist")
        return render(request, "admin/billing_settlement.html", {
            **self.admin_site.each_context(request), "payment": payment, "title": "Подтверждение операции",
        })
