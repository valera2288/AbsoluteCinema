from functools import wraps

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import logout
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth.views import INTERNAL_RESET_SESSION_TOKEN, LoginView, PasswordResetConfirmView, PasswordResetView
from django.contrib.sessions.models import Session
from django.db import IntegrityError, transaction
from django.db.models.functions import Lower
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods, require_POST

from accounts import services
from accounts.forms import (
    AccountCreationForm, GuardForm, PasswordConfirmation, PinForm, ProfileForm, ProfileNameForm,
    ViewerDetailsForm, ViewerLoginForm,
)


def viewer_required(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.viewer_device:
            return redirect("viewer-login")
        return view(request, *args, **kwargs)
    return never_cache(wrapped)


def render_form(request, form, title):
    return render(request, "accounts/form.html", {"form": form, "title": title})


def uniqueness_error(form, exception):
    diagnostic = getattr(exception.__cause__, "diag", None)
    constraint = getattr(diagnostic, "constraint_name", "")
    if constraint == "unique_lower_login" or constraint == "users_login_key":
        form.add_error("login", "Этот логин уже занят.")
    elif constraint == "unique_lower_email":
        form.add_error("email", "Этот email уже занят.")
    else:
        raise exception


@sensitive_post_parameters()
@never_cache
@require_http_methods(["GET", "POST"])
def register(request):
    form = AccountCreationForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                user = form.save()
                user.profiles.create(profile_name="Основной")
        except IntegrityError as exc:
            uniqueness_error(form, exc)
        else:
            messages.success(request, "Аккаунт создан. Войдите, чтобы открыть кабинет.")
            return redirect("viewer-login")
    return render_form(request, form, "Регистрация")


class ViewerLogin(LoginView):
    authentication_form = ViewerLoginForm
    template_name = "accounts/login.html"
    next_page = reverse_lazy("viewer-dashboard")
    extra_context = {"site_name": settings.SITE_NAME}

    def dispatch(self, request, *args, **kwargs):
        if request.viewer_device:
            return redirect("viewer-dashboard")
        if request.user.is_authenticated:
            return render(request, "accounts/staff_session.html")
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        services.start_device(self.request, form.get_user())
        return redirect(self.get_success_url())


class ResetRequest(PasswordResetView):
    template_name = "accounts/form.html"
    email_template_name = "accounts/reset_email.txt"
    subject_template_name = "accounts/reset_subject.txt"
    success_url = reverse_lazy("viewer-reset-sent")
    title = "Восстановление пароля"


class ResetConfirm(PasswordResetConfirmView):
    template_name = "accounts/reset_confirm.html"
    success_url = reverse_lazy("viewer-login")

    def form_valid(self, form):
        if not services.update_password(
            self.user.pk, form.cleaned_data["new_password1"],
            token=self.request.session[INTERNAL_RESET_SESSION_TOKEN],
        ):
            self.validlink = False
            return self.render_to_response(self.get_context_data())
        logout(self.request)
        messages.success(self.request, "Пароль обновлён. Войдите заново.")
        return redirect(self.success_url)


@viewer_required
@require_http_methods(["GET"])
def dashboard(request):
    from viewing.models import WatchHistory
    from catalog.queries import visible_movies

    live_sessions = Session.objects.filter(expire_date__gt=timezone.now()).values("session_key")
    continuing = WatchHistory.objects.filter(
        profile_id=request.viewer_device.active_profile_id, profile__user=request.user,
        movie__in=visible_movies(request.viewer_device), progress_sec__gt=0, is_completed=False,
    ).select_related("movie").order_by("-watched_at", "-pk")[:6]
    return render(request, "accounts/dashboard.html", {
        "continuing": continuing,
        "device": request.viewer_device,
        "profiles": request.user.profiles.all(),
        "devices": request.user.devices.filter(
            revoked_at__isnull=True, session_key__in=live_sessions,
        ).order_by("-last_seen_at"),
    })


@sensitive_post_parameters()
@viewer_required
@require_http_methods(["GET", "POST"])
def details(request):
    data = request.POST if request.method == "POST" else None
    if request.method == "POST":
        try:
            with transaction.atomic():
                user = services.locked_user(request.user.pk)
                form = ViewerDetailsForm(data, instance=user)
                if form.is_valid():
                    form.save()
                    messages.success(request, "Данные обновлены.")
                    return redirect("viewer-dashboard")
        except IntegrityError as exc:
            uniqueness_error(form, exc)
    else:
        form = ViewerDetailsForm(instance=request.user)
    return render_form(request, form, "Данные аккаунта")


@sensitive_post_parameters()
@viewer_required
@require_http_methods(["GET", "POST"])
def password_change(request):
    form = PasswordChangeForm(request.user, request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        if services.update_password(
            request.user.pk, form.cleaned_data["new_password1"], current_password=form.cleaned_data["old_password"],
        ):
            logout(request)
            messages.success(request, "Пароль изменён. Войдите заново.")
            return redirect("viewer-login")
        form.add_error("old_password", "Пароль уже изменился.")
    return render_form(request, form, "Смена пароля")


@sensitive_post_parameters()
@viewer_required
@require_http_methods(["GET", "POST"])
def account_delete(request):
    form = PasswordConfirmation(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        if services.delete_account(request.user.pk, form.cleaned_data["current_password"]):
            logout(request)
            messages.success(request, "Аккаунт и профили удалены.")
            return redirect("home")
        form.add_error("current_password", "Неверный пароль аккаунта.")
    return render_form(request, form, "Удалить аккаунт и профили")


@viewer_required
@require_POST
def device_revoke(request, device_id):
    if services.revoke_device(request.user.pk, device_id) == request.session.session_key:
        logout(request)
        return redirect("viewer-login")
    return redirect("viewer-dashboard")


@require_POST
def viewer_logout(request):
    if request.viewer_device:
        services.revoke_device(request.user.pk, request.viewer_device.pk)
    logout(request)
    return redirect("home")


PROFILE_FORMS = {
    "create": (ProfileForm, "Новый профиль"),
    "rename": (ProfileNameForm, "Переименовать профиль"),
    "switch": (GuardForm, "Выбрать профиль"),
    "delete": (GuardForm, "Подтвердить удаление профиля"),
    "pin": (PinForm, "Настроить PIN"),
}


@sensitive_post_parameters()
@viewer_required
@require_http_methods(["GET", "POST"])
def profiles(request, action, profile_id=None):
    if action not in PROFILE_FORMS or (action == "create") != (profile_id is None):
        raise Http404
    profile = None if profile_id is None else get_object_or_404(request.user.profiles, pk=profile_id)
    form_class, title = PROFILE_FORMS[action]
    initial = {"profile_name": profile.profile_name} if profile else {}
    form = form_class(request.POST if request.method == "POST" else None, initial=initial)
    if action in ("switch", "delete"):
        device = request.viewer_device
        needs_guard = device.kids_locked and (
            (action == "switch" and not profile.is_kids)
            or (action == "delete" and device.active_profile_id == profile.pk)
        )
        for field in form.fields:
            if not needs_guard or (field == "pin") != bool(device.guarded_profile and device.guarded_profile.pin_hash):
                form.fields[field].widget = forms.HiddenInput()
    if request.method == "POST" and form.is_valid():
        error = services.profile_action(request.user.pk, request.session.session_key, action, profile_id, **form.cleaned_data)
        if error:
            field = "current_password" if action == "pin" else None
            form.add_error(field, error)
        else:
            return redirect("viewer-dashboard")
    return render_form(request, form, f"{title}: {profile.profile_name}" if profile else title)
