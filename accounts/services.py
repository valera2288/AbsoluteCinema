from datetime import timedelta

from django.contrib.auth import login
from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.tokens import default_token_generator
from django.contrib.sessions.models import Session
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone

from accounts.models import Device, LoginThrottle, Profile, User
from accounts.throttle import subject_key


def locked_user(user_id):
    return get_object_or_404(User.objects.select_for_update(), pk=user_id, is_active=True)


@transaction.atomic
def start_device(request, authenticated):
    user = locked_user(authenticated.pk)
    if user.password != authenticated.password:
        raise PermissionDenied
    if request.user.is_authenticated:
        raise PermissionDenied
    profile = user.profiles.first()
    if profile is None:
        profile = user.profiles.create(profile_name="Основной")
    login(request, user, backend="accounts.backends.AccountBackend")
    request.session["viewer_device"] = True
    request.session.save()
    mobile = "mobile" in request.headers.get("User-Agent", "").lower()
    guard = profile if profile.is_kids and profile.pin_hash else None
    Device.objects.create(
        user=user, session_key=request.session.session_key, active_profile=profile,
        guarded_profile=guard, kids_locked=bool(guard),
        name="Мобильный браузер" if mobile else "Браузер",
        device_type="mobile" if mobile else "desktop",
    )


def revoke_devices(devices):
    from viewing.services import close_sessions

    close_sessions("device_revoked", device_id__in=devices.values("pk"))
    keys = list(devices.values_list("session_key", flat=True))
    devices.filter(revoked_at__isnull=True).update(revoked_at=timezone.now())
    Session.objects.filter(session_key__in=keys).delete()


@transaction.atomic
def revoke_device(user_id, device_id):
    user = locked_user(user_id)
    device = get_object_or_404(user.devices, pk=device_id)
    revoke_devices(user.devices.filter(pk=device_id))
    return device.session_key


@transaction.atomic
def update_password(user_id, password, *, current_password=None, token=None):
    user = locked_user(user_id)
    if token is not None:
        valid = default_token_generator.check_token(user, token)
    else:
        valid = check_password(current_password, user.password)
    if not valid:
        return False
    user.set_password(password)
    user.save(update_fields=["password"])
    revoke_devices(user.devices.all())
    return True


@transaction.atomic
def delete_account(user_id, password):
    user = locked_user(user_id)
    if not check_password(password, user.password):
        return False
    remove_locked_account(user)
    return True


def remove_locked_account(user):
    from billing.services import detach_billing
    from operations.services import anonymize_support

    revoke_devices(user.devices.all())
    detach_billing(user)
    anonymize_support(user)
    LoginThrottle.objects.filter(subject_key__in=[subject_key(user.login), subject_key(user.email)]).delete()
    user.delete()


def guard_error(user, device, pin, password):
    if not device.kids_locked:
        return None
    now = timezone.now()
    if user.pin_locked_until and user.pin_locked_until > now:
        return "Проверка PIN заблокирована на 15 минут."
    guard = device.guarded_profile
    if not guard or not guard.pin_hash:
        if not check_password(password, user.password):
            return "Подтвердите текущий пароль аккаунта."
    elif not check_password(pin, guard.pin_hash):
        user.pin_failed_attempts = (0 if user.pin_locked_until else user.pin_failed_attempts) + 1
        user.pin_locked_until = now + timedelta(minutes=15) if user.pin_failed_attempts >= 5 else None
        user.save(update_fields=["pin_failed_attempts", "pin_locked_until"])
        return "Проверка PIN заблокирована на 15 минут." if user.pin_locked_until else "Неверный PIN."
    user.pin_failed_attempts = 0
    user.pin_locked_until = None
    user.save(update_fields=["pin_failed_attempts", "pin_locked_until"])
    device.kids_locked = False
    device.guarded_profile = None
    return None


@transaction.atomic
def profile_action(user_id, session_key, action, profile_id=None, **values):
    user = locked_user(user_id)
    device = get_object_or_404(user.devices, session_key=session_key, revoked_at__isnull=True)
    if action == "create":
        if user.profiles.count() >= 5:
            return "Можно создать не более пяти профилей."
        user.profiles.create(profile_name=values["profile_name"], is_kids=values["is_kids"])
        return None
    profile = get_object_or_404(user.profiles, pk=profile_id)
    if action == "switch":
        from viewing.services import close_sessions

        if not profile.is_kids:
            error = guard_error(user, device, values["pin"], values["current_password"])
            if error:
                return error
        elif not device.kids_locked and profile.pin_hash:
            device.kids_locked = True
            device.guarded_profile = profile
        if device.active_profile_id != profile.pk:
            close_sessions("profile_switched", device=device)
        device.active_profile = profile
        device.save(update_fields=["active_profile", "guarded_profile", "kids_locked"])
    elif action == "delete":
        if user.profiles.count() == 1:
            return "Нельзя удалить последний профиль."
        if profile.is_kids and device.active_profile_id == profile.pk:
            error = guard_error(user, device, values["pin"], values["current_password"])
            if error:
                return error
            device.save(update_fields=["guarded_profile", "kids_locked"])
        profile.delete()
    elif action == "rename":
        profile.profile_name = values["profile_name"]
        profile.save(update_fields=["profile_name"])
    elif action == "pin":
        if not profile.is_kids:
            raise Http404
        if not check_password(values["current_password"], user.password):
            return "Неверный пароль аккаунта."
        profile.pin_hash = make_password(values["pin"]) if values["pin"] else ""
        profile.save(update_fields=["pin_hash"])
        if profile.pin_hash:
            from viewing.services import close_sessions

            affected = user.devices.filter(active_profile=profile, kids_locked=False, revoked_at__isnull=True)
            close_sessions("profile_guarded", device_id__in=affected.values("pk"))
            affected.update(
                kids_locked=True, guarded_profile=profile,
            )
    return None
