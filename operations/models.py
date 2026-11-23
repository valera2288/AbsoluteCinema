import uuid

from django.conf import settings
from django.db import models


class AuditEntry(models.Model):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    subject_user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    action = models.CharField(max_length=40)
    object_kind = models.CharField(max_length=30)
    object_ref = models.CharField(max_length=40)
    result_code = models.CharField(max_length=30)
    event_key = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "audit_log"
        verbose_name = "запись журнала"
        verbose_name_plural = "журнал операций"
        indexes = [models.Index(fields=["action", "-created_at"], name="audit_action_date")]


class Notification(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    subject = models.CharField(max_length=120)
    text = models.CharField(max_length=300)
    event_key = models.CharField(max_length=120)
    sent_at = models.DateTimeField(auto_now_add=True)
    is_read = models.BooleanField(default=False)

    class Meta:
        db_table = "notifications"
        constraints = [models.UniqueConstraint(fields=["user", "event_key"], name="notification_event_per_user")]
        indexes = [models.Index(fields=["user", "is_read", "-sent_at"], name="notification_inbox")]


class NotificationSettings(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    marketing_opt_in = models.BooleanField(default=False)

    class Meta:
        db_table = "notification_settings"


class SupportRequest(models.Model):
    class Status(models.TextChoices):
        NEW = "new", "Новое"
        IN_PROGRESS = "in_progress", "В работе"
        CLOSED = "closed", "Закрыто"

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    subject = models.CharField("Тема", max_length=100, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.NEW)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "support_requests"
        verbose_name_plural = "обращения в поддержку"
        permissions = [
            ("reply_support", "Может отвечать на обращения"),
            ("send_test_newsletter", "Может отправлять тестовую рассылку"),
            ("export_reports", "Может выгружать отчёты"),
        ]
        indexes = [
            models.Index(fields=["owner", "-updated_at"], name="support_owner_recent"),
            models.Index(fields=["status", "updated_at"], name="support_queue_status"),
        ]
        constraints = [models.CheckConstraint(condition=(
            models.Q(status="closed", closed_at__isnull=False) | models.Q(status__in=["new", "in_progress"], closed_at__isnull=True)
        ), name="support_closed_date")]


class SupportMessage(models.Model):
    class AuthorKind(models.TextChoices):
        VIEWER = "viewer", "Зритель"
        STAFF = "staff", "Поддержка"

    class Reply(models.TextChoices):
        REVIEWED = "reviewed", "Обращение рассмотрено."
        RETRY = "retry", "Для решения требуется повторить действие."
        RESOLVED = "resolved", "Проблема устранена."

    request = models.ForeignKey(SupportRequest, on_delete=models.CASCADE, related_name="messages")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    author_kind = models.CharField(max_length=8, choices=AuthorKind.choices)
    body = models.TextField(max_length=2000, blank=True)
    reply_code = models.CharField(max_length=12, choices=Reply.choices, null=True, blank=True)
    request_key = models.UUIDField(null=True, blank=True, unique=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "support_messages"
        indexes = [models.Index(fields=["request", "created_at", "id"], name="support_message_order")]
        constraints = [models.CheckConstraint(condition=(
            models.Q(author_kind="viewer", reply_code__isnull=True) | models.Q(author_kind="staff", reply_code__isnull=False, reply_code__in=["reviewed", "retry", "resolved"])
        ), name="support_message_author_reply")]


class BackupRun(models.Model):
    class Status(models.TextChoices):
        RUNNING = "running", "Выполняется"
        READY = "ready", "Готова"
        FAILED = "failed", "Ошибка"

    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.RUNNING)
    archive_name = models.CharField(max_length=100, unique=True)
    error_code = models.CharField(max_length=32, blank=True)

    class Meta:
        db_table = "backup_runs"
        verbose_name_plural = "резервные копии"
        permissions = [("run_backup", "Может создавать резервную копию")]
        indexes = [models.Index(fields=["-started_at"], name="backup_run_recent")]
