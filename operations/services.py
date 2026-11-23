from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone

from accounts.models import User
from accounts.services import locked_user
from operations.models import Notification, NotificationSettings, SupportMessage, SupportRequest


@transaction.atomic
def create_request(user_id, subject, body):
    owner = locked_user(user_id)
    ticket = SupportRequest.objects.create(owner=owner, subject=subject)
    ticket.messages.create(author=owner, author_kind=SupportMessage.AuthorKind.VIEWER, body=body)
    return ticket


@transaction.atomic
def handle_request(actor_id, ticket_id, action, *, reply_code="", body="", request_key=None):
    owner_id = get_object_or_404(SupportRequest, pk=ticket_id).owner_id
    accounts = {user.pk: user for user in User.objects.select_for_update().filter(
        pk__in=[actor_id, owner_id],
    ).order_by("pk")}
    actor = accounts.get(actor_id)
    if not actor or not actor.is_active or not actor.is_staff or not actor.has_perm("operations.reply_support"):
        raise PermissionDenied
    ticket = get_object_or_404(SupportRequest.objects.select_for_update(), pk=ticket_id)
    has_reply = bool(reply_code)
    body = body.strip()
    if len(body) > 2000:
        raise ValidationError("Дополнение не должно превышать 2000 символов.")
    if body and not has_reply:
        raise ValidationError("Сначала выберите фиксированный ответ.")
    if has_reply and (reply_code not in SupportMessage.Reply.values or request_key is None):
        raise ValidationError("Некорректный ответ.")
    if has_reply and SupportMessage.objects.filter(request=ticket, author=actor, request_key=request_key).exists():
        return
    if ticket.owner_id != owner_id or (has_reply and not ticket.owner_id):
        raise ValidationError("Владелец обращения удалён.")
    status = SupportRequest.Status
    if ticket.status == status.CLOSED:
        raise ValidationError("Обращение уже закрыто.")
    if action not in ("work", "close"):
        raise ValidationError("Неизвестное действие.")
    if action == "close" and not has_reply and not ticket.messages.filter(author_kind="staff").exists():
        raise ValidationError("Чтобы закрыть обращение, сначала ответьте.")
    if has_reply:
        message = ticket.messages.create(author=actor, author_kind=SupportMessage.AuthorKind.STAFF,
                                         reply_code=reply_code, body=body, request_key=request_key)
        Notification.objects.create(
            user_id=ticket.owner_id, event_key=f"support:{message.pk}", subject="Ответ поддержки",
            text=f"В обращении №{ticket.pk} появился ответ.",
        )
    ticket.status = status.CLOSED if action == "close" else status.IN_PROGRESS
    ticket.closed_at = timezone.now() if action == "close" else None
    ticket.save(update_fields=["status", "closed_at", "updated_at"])


def anonymize_support(user):
    tickets = list(SupportRequest.objects.select_for_update().filter(owner=user).order_by("pk").values_list("pk", flat=True))
    SupportMessage.objects.filter(request_id__in=tickets).update(author=None, body="")
    SupportRequest.objects.filter(pk__in=tickets).update(subject="")
    SupportMessage.objects.filter(author=user).update(body="")


@transaction.atomic
def set_marketing(user_id, enabled):
    owner = locked_user(user_id)
    NotificationSettings.objects.update_or_create(user=owner, defaults={"marketing_opt_in": enabled})


def send_newsletter(actor, event_key):
    if not actor.is_active or not actor.is_staff or not actor.has_perm("operations.send_test_newsletter"):
        raise PermissionDenied
    recipients = list(NotificationSettings.objects.filter(marketing_opt_in=True).values_list("user_id", flat=True))
    delivered = 0
    for recipient in recipients:
        with transaction.atomic():
            owner = User.objects.select_for_update().filter(pk=recipient, is_active=True).first()
            if owner and NotificationSettings.objects.filter(user=owner, marketing_opt_in=True).exists():
                _, created = Notification.objects.get_or_create(
                    user=owner, event_key=f"newsletter:{event_key}", defaults={
                        "subject": "Новости Absolute Cinema", "text": "Это тестовая рассылка для зрителей, которые согласились получать новости.",
                    },
                )
                delivered += created
    return delivered
