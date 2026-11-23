from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from django.utils.crypto import salted_hmac

from accounts.models import LoginThrottle

WINDOW = timedelta(minutes=15)


def subject_key(identifier):
    return salted_hmac("accounts.login.subject", identifier.strip().lower(), algorithm="sha256").hexdigest()


@transaction.atomic
def limited_login(request, identifier, authenticate):
    ip = request.META.get("REMOTE_ADDR", "")
    ip_key = salted_hmac("accounts.login.ip", ip, algorithm="sha256").hexdigest()
    ip_row, _ = LoginThrottle.objects.select_for_update().get_or_create(
        scope="ip", subject_key="", ip_key=ip_key, defaults={"expires_at": timezone.now},
    )
    now = timezone.now()
    expiry = now + WINDOW
    attempts = [value for value in ip_row.attempts if value > (now - WINDOW).timestamp()]
    if len(attempts) >= 30:
        return None
    ip_row.attempts = [*attempts, now.timestamp()]
    ip_row.expires_at = expiry
    ip_row.save(update_fields=["attempts", "expires_at"])
    pair, _ = LoginThrottle.objects.select_for_update().get_or_create(
        scope="pair", subject_key=subject_key(identifier), ip_key=ip_key, defaults={"expires_at": expiry},
    )
    if pair.locked_until and pair.locked_until > now:
        return None
    user = authenticate()
    attempts = [] if user or pair.locked_until else [
        value for value in pair.attempts if value > (now - WINDOW).timestamp()
    ]
    if not user:
        attempts.append(now.timestamp())
    pair.attempts = attempts
    pair.locked_until = expiry if len(attempts) >= 5 else None
    pair.expires_at = expiry
    pair.save(update_fields=["attempts", "locked_until", "expires_at"])
    return user
