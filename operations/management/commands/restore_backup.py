import re
import subprocess
from uuid import uuid4

import psycopg
from django.core.management.base import BaseCommand, CommandError

from operations.backup import archive_path, backup_root, invoke, pg_env


class Command(BaseCommand):
    help = "Восстанавливает локальный архив только в отдельную пустую учебную базу restore_*."

    def add_arguments(self, parser):
        parser.add_argument("archive")
        parser.add_argument("database")

    def handle(self, *args, **options):
        name = options["database"]
        env = pg_env()
        if (not re.fullmatch(r"restore_[a-z0-9_]{1,48}", name) or name == env.get("PGDATABASE")
                or env.get("PGHOST") not in ("127.0.0.1", "localhost", "::1")):
            raise CommandError("Нужна отдельная локальная база с именем restore_*.")
        try:
            path = archive_path(backup_root(), options["archive"])
            if not path.is_file():
                raise FileNotFoundError
            env["PGDATABASE"] = name
            target = {key: env.get(variable) for key, variable in (
                ("host", "PGHOST"), ("port", "PGPORT"), ("dbname", "PGDATABASE"),
                ("user", "PGUSER"), ("password", "PGPASSWORD"), ("passfile", "PGPASSFILE"),
                ("sslmode", "PGSSLMODE"),
            ) if env.get(variable)}
            with psycopg.connect(**target, connect_timeout=10, autocommit=True) as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_lock(1724709150)")
                    cursor.execute(
                        "SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname NOT LIKE 'pg_%' "
                        "AND nspname NOT IN ('public', 'information_schema')) OR EXISTS("
                        "SELECT 1 FROM pg_depend WHERE refobjid = 'public'::regnamespace "
                        "AND refclassid = 'pg_namespace'::regclass)"
                    )
                    if cursor.fetchone()[0]:
                        raise CommandError("Целевая учебная база не пуста. Восстановление отменено.")
                    invoke("pg_restore", ["--no-password", "--exit-on-error", "--single-transaction",
                                           "--no-owner", "--no-privileges", "--dbname", name, str(path)], env=env)
                    cursor.execute(
                        "INSERT INTO public.audit_log (action, object_kind, object_ref, result_code, event_key, created_at)"
                        " VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)",
                        ("backup.restore", "database", name, "ok", uuid4()),
                    )
        except (ValueError, OSError, subprocess.SubprocessError, psycopg.Error) as exc:
            raise CommandError("Восстановление или запись результата не удались. Проверьте архив и учебную базу.") from None
        self.stdout.write(self.style.SUCCESS(f"Архив восстановлен, результат записан в журнал базы {name}."))
