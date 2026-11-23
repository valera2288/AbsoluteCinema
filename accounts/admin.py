from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.db import transaction

from accounts.forms import AccountChangeForm, AccountCreationForm, AdminLoginForm
from accounts.models import User
from accounts.services import remove_locked_account, revoke_devices
from operations.models import AuditEntry

admin.site.login_form = AdminLoginForm


@admin.register(User)
class AccountAdmin(UserAdmin):
    form = AccountChangeForm
    add_form = AccountCreationForm
    list_display = ("login", "email", "full_name", "is_staff", "is_active", "can_post_reviews")
    search_fields = ("login", "email", "full_name")
    ordering = ("login",)
    actions = None
    readonly_fields = ("last_login", "date_joined")
    fieldsets = (
        (None, {"fields": ("login", "email", "password")}),
        ("Данные аккаунта", {"fields": ("full_name", "phone", "can_post_reviews")}),
        ("Доступ", {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")}),
        ("Даты", {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = (
        (None, {"fields": ("login", "email", "full_name", "phone", "password1", "password2")}),
    )

    def has_add_permission(self, request):
        return request.user.is_superuser and super().has_add_permission(request)

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser and super().has_delete_permission(request, obj)

    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        if request.method == "POST" and object_id and object_id.isdecimal() and len(object_id) <= 19:
            with transaction.atomic():
                list(User.objects.select_for_update().filter(pk=object_id, pk__lte=9223372036854775807))
                return super().changeform_view(request, object_id, form_url, extra_context)
        return super().changeform_view(request, object_id, form_url, extra_context)

    @transaction.atomic
    def delete_model(self, request, obj):
        user = User.objects.select_for_update().get(pk=obj.pk)
        remove_locked_account(user)

    @transaction.atomic
    def save_model(self, request, obj, form, change):
        previous = User.objects.select_for_update().get(pk=obj.pk) if change else None
        super().save_model(request, obj, form, change)
        if previous and previous.is_active and not obj.is_active:
            revoke_devices(obj.devices.all())
            AuditEntry.objects.create(actor=request.user, action="account.block", object_kind="account",
                                      object_ref="", result_code="ok", subject_user=obj)
        if previous and not previous.is_active and obj.is_active:
            AuditEntry.objects.create(actor=request.user, action="account.unblock", object_kind="account",
                                      object_ref="", result_code="ok", subject_user=obj)

    def save_related(self, request, form, formsets, change):
        user = form.instance
        before = (set(user.groups.values_list("pk", flat=True)), set(user.user_permissions.values_list("pk", flat=True)))
        super().save_related(request, form, formsets, change)
        after = (set(user.groups.values_list("pk", flat=True)), set(user.user_permissions.values_list("pk", flat=True)))
        if before != after or {"is_staff", "is_superuser"}.intersection(form.changed_data):
            AuditEntry.objects.create(actor=request.user, subject_user=user, action="account.roles",
                                      object_kind="account", object_ref="", result_code="ok")
