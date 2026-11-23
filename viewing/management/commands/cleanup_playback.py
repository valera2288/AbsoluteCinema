from django.core.management.base import BaseCommand

from viewing.services import cleanup_sessions


class Command(BaseCommand):
    help = "Удаляет старые сеансы, сохраняя историю и факты просмотра."

    def handle(self, *args, **options):
        self.stdout.write(f"Удалено сеансов: {cleanup_sessions()}")
