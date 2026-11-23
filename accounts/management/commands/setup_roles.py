from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

ROLE_PERMISSIONS = {
    "Администраторы": "view_user",
    "Модераторы": "restrict_review_authors",
    "Пробный просмотр": "view_media_probe",
}


class Command(BaseCommand):
    help = "Создаёт базовые группы и добавляет недостающие разрешения."

    @transaction.atomic
    def handle(self, *args, **options):
        for name, codename in ROLE_PERMISSIONS.items():
            try:
                permission = Permission.objects.get(
                    content_type__app_label="accounts", content_type__model="user", codename=codename,
                )
            except Permission.DoesNotExist as exc:
                raise CommandError("Сначала выполните migrate.") from exc
            group, _ = Group.objects.get_or_create(name=name)
            group.permissions.add(permission)
            if name == "Модераторы":
                group.permissions.add(*Permission.objects.filter(content_type__app_label="catalog",
                    content_type__model="review", codename__in=("view_review_queue", "decide_review", "remove_review")))
            if name == "Администраторы":
                catalog_permissions = Permission.objects.filter(
                    content_type__app_label__in=("catalog", "billing"),
                    content_type__model__in=("genre", "movie", "moviemedia", "tariff", "promocode"),
                )
                audit_permission = Permission.objects.filter(
                    content_type__app_label="operations", codename="view_auditentry",
                )
                group.permissions.add(*catalog_permissions, *audit_permission)
                group.permissions.add(*Permission.objects.filter(content_type__app_label="billing",
                    codename__in=("view_subscription", "view_payment", "refund_payment")))
                group.permissions.add(*Permission.objects.filter(content_type__app_label="operations",
                    codename__in=("view_supportrequest", "reply_support", "send_test_newsletter", "export_reports",
                                  "view_backuprun", "run_backup")))
        self.stdout.write(self.style.SUCCESS("Базовые группы настроены."))
