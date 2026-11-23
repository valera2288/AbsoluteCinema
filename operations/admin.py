from uuid import uuid4

from django.contrib import admin, messages
from django.conf import settings
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path
from django.utils.decorators import method_decorator
from django.views.decorators.http import require_http_methods, require_safe

from operations.backup import create_backup
from operations.demo import populate
from operations.forms import ReplyForm, ReportForm
from operations.models import AuditEntry, BackupRun, SupportRequest
from operations.reports import export_report, report
from operations.services import handle_request, send_newsletter


class ReadOnlyAdmin(admin.ModelAdmin):
    actions = None

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AuditEntry)
class AuditAdmin(ReadOnlyAdmin):
    list_display = ("created_at", "actor", "subject_user", "action", "object_kind", "object_ref", "result_code")
    list_filter = ("action", "result_code", "created_at")
    date_hierarchy = "created_at"
    ordering = ("-created_at", "-pk")


@admin.register(SupportRequest)
class SupportAdmin(ReadOnlyAdmin):
    list_display = ("id", "subject", "owner", "status", "updated_at")
    list_filter = ("status", "updated_at")
    list_select_related = ("owner",)
    ordering = ("status", "-updated_at")

    def get_urls(self):
        return [
            path("reports/", self.admin_site.admin_view(self.reports), name="operations_reports"),
            path("newsletter/", self.admin_site.admin_view(self.newsletter), name="operations_newsletter"),
            path("demo/", self.admin_site.admin_view(self.demo), name="operations_demo"),
        ] + super().get_urls()

    @method_decorator(require_http_methods(["GET", "POST"]))
    def demo(self, request):
        if not settings.DEBUG or not request.user.is_superuser:
            raise PermissionDenied
        if request.method == "POST":
            from django.db import IntegrityError

            try:
                count = populate(request.user)
            except (ValidationError, IntegrityError):
                messages.error(request, "Набор не добавлен: конфликт с текущими настройками тарифов, промокодов или аккаунтов.")
            else:
                messages.success(request, f"Добавлено демонстрационных зрителей: {count}.")
            return redirect("admin:index")
        return render(request, "admin/operations/action.html", {
            **self.admin_site.each_context(request), "title": "Демонстрационные данные",
            "description": "Создать вымышленных зрителей, тестовые платежи и подписки, отзывы, обращения и историю просмотров. Существующие аккаунты не изменятся.",
            "submit_label": "Добавить учебные данные",
        })

    @method_decorator(require_http_methods(["GET", "POST"]))
    def change_view(self, request, object_id, form_url="", extra_context=None):
        if not object_id.isascii() or not object_id.isdecimal() or len(object_id) > 19 or int(object_id) > 2**63-1:
            from django.http import Http404

            raise Http404
        ticket = get_object_or_404(SupportRequest, pk=object_id)
        if not self.has_view_permission(request, ticket):
            raise PermissionDenied
        allowed = request.user.has_perm("operations.reply_support")
        form = ReplyForm(request.POST if request.method == "POST" else None, initial={"request_key": uuid4()})
        if request.method == "POST":
            if not allowed:
                raise PermissionDenied
            if form.is_valid():
                try:
                    handle_request(request.user.pk, ticket.pk, **form.cleaned_data)
                except ValidationError as exc:
                    form.add_error(None, exc)
                else:
                    messages.success(request, "Обращение обновлено.")
                    return redirect("admin:operations_supportrequest_change", object_id=ticket.pk)
        return render(request, "admin/operations/ticket.html", {
            **self.admin_site.each_context(request), "title": f"Обращение №{ticket.pk}", "ticket": ticket,
            "entries": ticket.messages.order_by("created_at", "pk"), "form": form,
            "allow_reply": allowed and ticket.status != "closed",
        })

    @method_decorator(require_safe)
    def reports(self, request):
        if not request.user.has_perm("operations.export_reports"):
            raise PermissionDenied
        form = ReportForm(request.GET or ReportForm.default())
        headers, rows = (), ()
        if form.is_valid():
            values = form.cleaned_data
            headers, rows = report(**values)
            file_format = request.GET.get("format")
            if file_format in ("csv", "xlsx"):
                return export_report(**values, file_format=file_format, headers=headers, rows=rows)
        return render(request, "admin/operations/reports.html", {
            **self.admin_site.each_context(request), "title": "Отчёты", "form": form, "headers": headers, "rows": rows,
        })

    @method_decorator(require_http_methods(["GET", "POST"]))
    def newsletter(self, request):
        if not request.user.has_perm("operations.send_test_newsletter"):
            raise PermissionDenied
        if request.method == "POST":
            try:
                payload = signing.loads(request.POST.get("token", ""), salt="operations.newsletter", max_age=1800)
            except signing.BadSignature:
                messages.error(request, "Форма устарела. Откройте её заново.")
            else:
                if payload["actor"] != request.user.pk:
                    raise PermissionDenied
                count = send_newsletter(request.user, payload["event"])
                messages.success(request, f"Новых доставок: {count}.")
            return redirect("admin:operations_newsletter")
        return render(request, "admin/operations/action.html", {
            **self.admin_site.each_context(request), "title": "Тестовая рассылка",
            "description": "Новости получат только зрители, которые включили рассылки.",
            "token": signing.dumps({"actor": request.user.pk, "event": uuid4().hex}, salt="operations.newsletter"),
            "submit_label": "Отправить",
        })


@admin.register(BackupRun)
class BackupAdmin(ReadOnlyAdmin):
    list_display = ("archive_name", "status", "started_at", "finished_at", "error_code")
    list_filter = ("status",)
    ordering = ("-started_at",)

    def get_urls(self):
        return [path("run/", self.admin_site.admin_view(self.run), name="operations_run_backup")] + super().get_urls()

    @method_decorator(require_http_methods(["GET", "POST"]))
    def run(self, request):
        if not request.user.has_perm("operations.run_backup"):
            raise PermissionDenied
        if request.method == "POST":
            result = create_backup(request.user)
            if result.status == BackupRun.Status.READY:
                messages.success(request, "Архив создан и проверен.")
            else:
                messages.error(request, f"Не удалось создать архив: {result.error_code}.")
            return redirect("admin:operations_backuprun_changelist")
        return render(request, "admin/operations/action.html", {
            **self.admin_site.each_context(request), "title": "Резервная копия",
            "description": "Копия содержит данные PostgreSQL, но не медиафайлы.",
            "submit_label": "Создать копию",
        })
