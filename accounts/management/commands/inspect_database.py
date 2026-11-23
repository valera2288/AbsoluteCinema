from django.core.management.base import BaseCommand
from django.db import connection
from django.db.migrations.recorder import MigrationRecorder


class Command(BaseCommand):
    help = "Показывает подключённую PostgreSQL, пользовательские таблицы и записанные миграции."

    def handle(self, *args, **options):
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_database(), current_setting('port'), version()")
            database, port, version = cursor.fetchone()
            self.stdout.write(f"{database}:{port} — {version}")
            cursor.execute(
                "SELECT schemaname, tablename FROM pg_tables "
                "WHERE schemaname NOT IN ('pg_catalog', 'information_schema') "
                "ORDER BY schemaname, tablename"
            )
            for schema, table in cursor.fetchall():
                self.stdout.write(f"  {schema}.{table}")
        applied = MigrationRecorder(connection).applied_migrations()
        self.stdout.write(f"Записанных миграций: {len(applied)}")
        for app, migration in sorted(applied):
            self.stdout.write(f"  {app}.{migration}")
