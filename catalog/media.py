import json
import re
import subprocess
from decimal import Decimal, InvalidOperation
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from django.conf import settings
from django.contrib.staticfiles import finders
from django.core.exceptions import ValidationError
from django.db import transaction
from PIL import Image

from catalog.models import MovieMedia

MIME_TYPES = {
    "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp",
    "mp4": "video/mp4", "vtt": "text/vtt",
}
IMAGE_FORMATS = {"jpeg", "png", "webp"}
MIB = 1024 * 1024
LIMITS = {"poster": 5 * MIB, "banner": 5 * MIB, "trailer": 100 * MIB, "video": 1024 * MIB, "subtitle": MIB}
TIMESTAMP = r"(?:(?:[0-9]{2,}):)?[0-5][0-9]:[0-5][0-9]\.[0-9]{3}"


def media_path(key):
    root = settings.CATALOG_MEDIA_ROOT.resolve()
    public = [settings.STATIC_ROOT, *settings.STATICFILES_DIRS]
    for finder in finders.get_finders():
        public.extend(storage.location for storage in finder.storages.values())
    for directory in public:
        directory = Path(directory).resolve()
        if root.is_relative_to(directory) or directory.is_relative_to(root):
            raise ValidationError("Хранилище каталога должно быть отделено от публичной статики.")
    path = (root / key).resolve()
    if path.parent != root or not re.fullmatch(r"[a-f0-9]{32}\.(jpeg|png|webp|mp4|vtt)", key):
        raise ValidationError("Неверный ключ медиафайла.")
    return path


def inspect_mp4(path):
    try:
        result = subprocess.run(
            [settings.FFPROBE_BIN, "-v", "error", "-protocol_whitelist", "file", "-f", "mov",
             "-enable_drefs", "0", "-show_entries",
             "format=format_name,duration:format_tags=major_brand:stream=codec_type,codec_name,width,height:stream_disposition=attached_pic",
             "-of", "json", str(path)],
            capture_output=True, check=True, timeout=30, encoding="utf-8",
        )
        payload = json.loads(result.stdout)
        if result.stderr:
            raise ValueError
        metadata = payload["format"]
        duration = Decimal(metadata["duration"])
        if ("mp4" not in metadata["format_name"].split(",")
                or not re.fullmatch(r"(isom|iso[2-9]|mp4[12]|avc1|M4V |dash|MSNV)", metadata.get("tags", {}).get("major_brand", ""))
                or not duration.is_finite() or not 0 < duration < 1_000_000):
            raise ValueError
    except FileNotFoundError as exc:
        raise ValidationError("FFprobe не найден. Установите его по README; видео не сохранено.") from exc
    except (ValueError, KeyError, InvalidOperation, OSError, subprocess.SubprocessError) as exc:
        raise ValidationError("Нужен читаемый MP4 с положительной длительностью.") from exc
    streams = payload.get("streams", [])
    video = [stream for stream in streams if stream["codec_type"] == "video"
             and not stream.get("disposition", {}).get("attached_pic")]
    if not video or any(stream["codec_name"] != "h264" or not stream.get("width") or not stream.get("height") for stream in video):
        raise ValidationError("MP4 должен содержать видеодорожку H.264.")
    if any(stream.get("codec_type") == "audio" and stream.get("codec_name") != "aac" for stream in streams):
        raise ValidationError("Если в MP4 есть звук, нужен кодек AAC.")
    return duration.quantize(Decimal("0.000001"))


def inspect_vtt(path):
    def milliseconds(mark):
        parts = mark.split(":")
        hours, minutes, seconds = (["0"] + parts)[-3:]
        return int(hours) * 3600000 + int(minutes) * 60000 + int(seconds.replace(".", ""))

    try:
        blocks = re.split(r"\n[ \t]*\n", path.read_text(encoding="utf-8-sig").strip("\n"))
        if not re.fullmatch(r"WEBVTT(?:[ \t][^\n]*)?", blocks[0].split("\n", 1)[0]) or "\x00" in "\n".join(blocks):
            raise ValueError
        cues = 0
        previous = -1
        for block in blocks[1:]:
            lines = block.splitlines()
            if not lines or re.match(r"NOTE(?:[ \t]|$)", lines[0]) or lines[0] in {"STYLE", "REGION"}:
                continue
            index = 0 if "-->" in lines[0] else 1
            match = re.fullmatch(rf"({TIMESTAMP})[ \t]+-->[ \t]+({TIMESTAMP})(?:[ \t]+[^\n]+)?", lines[index])
            if not match or len(lines) <= index + 1:
                raise ValueError
            start, end = map(milliseconds, match.groups())
            if end <= start or start < previous:
                raise ValueError
            previous = start
            cues += 1
        if not cues:
            raise ValueError
    except (UnicodeError, ValueError, IndexError) as exc:
        raise ValidationError("Нужен WebVTT в UTF-8 с корректными временными метками.") from exc


def inspect_file(path, kind, extension):
    size = path.stat().st_size
    if not 0 < size <= LIMITS[kind]:
        raise ValidationError("Файл пуст или превышает допустимый размер.")
    duration = None
    if kind in ("poster", "banner"):
        try:
            with Image.open(path) as image:
                actual = image.format.lower()
                if actual not in IMAGE_FORMATS or actual != extension:
                    raise ValueError
                image.verify()
            with Image.open(path) as image:
                image.load()
        except (OSError, ValueError, Image.DecompressionBombError) as exc:
            raise ValidationError("Нужно исправное изображение JPEG, PNG или WebP.") from exc
    elif kind in ("trailer", "video") and extension == "mp4":
        duration = inspect_mp4(path)
    elif kind == "subtitle" and extension == "vtt":
        inspect_vtt(path)
    else:
        raise ValidationError("Расширение не соответствует виду материала.")
    return size, duration


def validate_upload(upload, kind):
    extension = Path(upload.name).suffix.lower().lstrip(".")
    extension = "jpeg" if extension == "jpg" else extension
    expected = MIME_TYPES.get(extension)
    if not expected or upload.content_type not in (expected, "application/octet-stream", None):
        raise ValidationError("Заявленный MIME-тип не соответствует расширению.")
    if not 0 < upload.size <= LIMITS[kind]:
        raise ValidationError("Файл пуст или превышает допустимый размер.")
    with TemporaryDirectory() as temporary:
        path = Path(temporary) / f"candidate.{extension}"
        try:
            with path.open("wb") as target:
                size = 0
                for chunk in upload.chunks():
                    size += len(chunk)
                    if size > LIMITS[kind]:
                        raise ValidationError("Превышен допустимый размер файла.")
                    target.write(chunk)
            size, duration = inspect_file(path, kind, extension)
        finally:
            upload.seek(0)
    return extension, size, duration


def save_upload(media, upload, metadata):
    previous = media.storage_key
    extension, media.size, media.duration = metadata
    media.file_format = extension
    media.version_id = uuid4()
    media.storage_key = f"{media.version_id.hex}.{extension}"
    target = media_path(media.storage_key)
    target.parent.mkdir(parents=True, exist_ok=True)
    destination = target.open("xb")
    try:
        with destination:
            for chunk in upload.chunks():
                destination.write(chunk)
        media.save()
    except Exception:
        remove_file(media.storage_key)
        raise
    if previous:
        transaction.on_commit(lambda: remove_file(previous))


def remove_file(key):
    try:
        media_path(key).unlink(missing_ok=True)
    except (OSError, ValidationError):
        pass


def verify_media(media):
    try:
        size, duration = inspect_file(media_path(media.storage_key), media.kind, media.file_format)
        if (size, duration) != (media.size, media.duration):
            raise ValidationError("Файл изменился вне операции загрузки.")
    except OSError as exc:
        raise ValidationError("Файл недоступен в хранилище.") from exc
