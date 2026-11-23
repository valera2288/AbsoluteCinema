from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from accounts.models import User
from billing.models import Subscription
from billing.services import expire_due, renew


class Command(BaseCommand):
    help = "Завершает просроченные периоды и проводит учебное автопродление."

    def handle(self, *args, **options):
        now = timezone.now()
        owners = Subscription.objects.filter(
            Q(status="active") | Q(status="expired", auto_renew=True, renewal_requires_retry=False),
            user__isnull=False, end_at__lte=now,
        ).order_by().values_list("user_id", flat=True).distinct()
        count = 0
        for owner_id in list(owners):
            with transaction.atomic():
                owner = User.objects.select_for_update().filter(pk=owner_id).first()
                if owner:
                    expire_due(owner, timezone.now())
                    if owner.is_active and renew(owner.pk):
                        count += 1
        self.stdout.write(f"Учебных продлений: {count}")
