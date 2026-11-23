import os
import re
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import uuid4

from django.conf import settings
from django.contrib.staticfiles import finders
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.utils import timezone

from operations.models import BackupRun


def backup_root():
    root = settings.BACKUP_ROOT.resolve()
    public = [settings.STATIC_ROOT, settings.CATALOG_MEDIA_ROOT, *settings.STATICFILES_DIRS]
    for finder in finders.get_finders():
        public.extend(storage.location for storage in finder.storages.values())
    if any(root.is_relative_to(Path(path).resolve()) or Path(path).resolve().is_relative_to(root) for path in public):
        raise ValueError
    root.mkdir(parents=True, exist_ok=True)
    return root


def pg_command(name):
    if settings.POSTGRES_BIN_DIR:
        directory = Path(settings.POSTGRES_BIN_DIR)
        return str(directory / (name + ".exe" if os.name == "nt" else name))
    detected = shutil.which(name)
    if detected:
        return detected
    if os.name == "nt" and settings.DEBUG:
        bundled = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "PostgreSQL" / "17" / "bin" / f"{name}.exe"
        if bundled.is_file():
            return str(bundled)
    return name


def pg_env():
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("PG")}
    for setting, key in (("HOST", "PGHOST"), ("PORT", "PGPORT"), ("NAME", "PGDATABASE"),
                         ("USER", "PGUSER"), ("PASSWORD", "PGPASSWORD")):
        value = connection.settings_dict[setting]
        if value:
            env[key] = str(value)
    for key in ("PGPASSFILE", "PGSSLMODE"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    env["PGCONNECT_TIMEOUT"] = "10"
    return env


def invoke(name, args, *, env=None):
    subprocess.run([pg_command(name), *args], env=env, stdin=subprocess.DEVNULL,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=300)


def archive_path(root, name):
    if not re.fullmatch(r"cinema-[0-9]{8}-[0-9]{6}-[0-9a-f]{32}\.dump", name):
        raise ValueError
    path = root / name
    if path.is_symlink() or path.resolve().parent != root:
        raise ValueError
    return path


def create_backup(actor):
    if not actor.is_active or not actor.is_staff or not actor.has_perm("operations.run_backup"):
        raise PermissionDenied
    name = f"cinema-{timezone.localtime():%Y%m%d-%H%M%S}-{uuid4().hex}.dump"
    run = BackupRun.objects.create(requested_by=actor, archive_name=name)
    temporary = None
    try:
        root = backup_root()
        stale = BackupRun.objects.filter(status=BackupRun.Status.RUNNING,
                                         started_at__lt=timezone.now() - timedelta(minutes=15))
        for previous in stale.iterator():
            try:
                invoke("pg_restore", ["--list", str(archive_path(root, previous.archive_name))])
            except (ValueError, OSError, subprocess.SubprocessError):
                status, code = BackupRun.Status.FAILED, "interrupted"
            else:
                status, code = BackupRun.Status.READY, ""
            stale.filter(pk=previous.pk).update(status=status, error_code=code, finished_at=timezone.now())
        with NamedTemporaryFile(dir=root, prefix="backup-", suffix=".partial", delete=False) as file:
            temporary = Path(file.name)
        invoke("pg_dump", ["--no-password", "--format=custom", "--file", str(temporary)], env=pg_env())
        invoke("pg_restore", ["--list", str(temporary)])
        os.replace(temporary, archive_path(root, name))
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        run.status = BackupRun.Status.FAILED
        run.error_code = "tool_unavailable" if isinstance(exc, FileNotFoundError) else "backup_failed"
    else:
        run.status = BackupRun.Status.READY
    finally:
        if temporary:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    run.finished_at = timezone.now()
    run.save(update_fields=["status", "error_code", "finished_at"])
    return run
