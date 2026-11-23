from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import UploadedFile
from django.db import transaction

from catalog.media import remove_file, save_upload, validate_upload
from catalog.models import Genre, Movie, MovieGenre, MovieMedia
from catalog.services import set_publication


def publish_defense_catalog(actor):
    if not actor.is_active or not actor.is_staff or not actor.is_superuser:
        raise PermissionDenied
    folder = settings.BASE_DIR / "private" / "open-films"
    poster_folder = settings.BASE_DIR / "private" / "official-posters"
    specs = (
        ("Большой Бак Банни", 2008, 10, 6, "Комедия",
         "Добродушному кролику надоедают выходки троих лесных задир. Полная версия с оригинальными титрами. "
         "Blender Foundation, CC BY 3.0: https://peach.blender.org/about/. "
         "Афиша и трейлер — проект Peach; видео перекодировано без обрезки.",
         poster_folder / "bunny.jpg", folder / "bunny-trailer.mp4", folder / "bunny-full.mp4", False, (595, 598)),
        ("Синтел", 2010, 15, 16, "Фэнтези",
         "Юная путешественница отправляется на поиски дракона, которого когда-то спасла. "
         "Полная версия с оригинальными титрами. Blender Foundation, CC BY 3.0: https://durian.blender.org/sharing/.",
         poster_folder / "sintel.jpg", folder / "sintel-trailer.mp4", folder / "sintel-full.mp4", False, (887, 889)),
        ("Демонстрационный ролик", 2026, 1, 0, "Учебные материалы",
         "Оригинальный тестовый ролик Absolute Cinema: 1 минута 20 секунд, цветовые полосы и тон. "
         "Подготовлен специально для проверки скачивания и просмотра без сети.",
         folder / "demo-poster.jpg", folder / "demo-trailer.mp4",
         settings.BASE_DIR / "private" / "online-cinema-probe.mp4", True, (79, 81)),
    )
    stored = []
    try:
        with transaction.atomic():
            actor = get_user_model().objects.select_for_update().get(pk=actor.pk)
            if not actor.is_active or not actor.is_staff or not actor.is_superuser:
                raise PermissionDenied
            for title, year, duration, age, genre_name, description, poster, trailer, video, downloadable, limits in specs:
                film = Movie.objects.filter(title=title, release_year=year, description=description).first()
                if not film:
                    genre, _ = Genre.objects.get_or_create(name=genre_name)
                    film = Movie.objects.create(title=title, release_year=year, duration_min=duration,
                                                age_rating=age, description=description)
                    MovieGenre.objects.create(movie=film, genre=genre)
                    for kind, path, mime in (("poster", poster, "image/jpeg"), ("trailer", trailer, "video/mp4"),
                                              ("video", video, "video/mp4")):
                        with path.open("rb") as source:
                            upload = UploadedFile(source, name=path.name, content_type=mime, size=path.stat().st_size)
                            media = MovieMedia(movie=film, kind=kind, allow_download=downloadable and kind == "video")
                            metadata = validate_upload(upload, kind)
                            if kind == "video" and not limits[0] <= metadata[2] <= limits[1]:
                                raise ValidationError(f"Длительность файла не соответствует карточке «{title}».")
                            if kind == "video" and downloadable and not 5_000_000 <= metadata[1] <= 20_000_000:
                                raise ValidationError("Ролик для проверки скачивания должен занимать 5–20 МБ.")
                            save_upload(media, upload, metadata)
                            stored.append(media.storage_key)
                set_publication(actor, film.pk, True)
            fragments = Movie.objects.filter(is_published=True, title__in=["Большой Бак Банни", "Синтел"],
                                             description__contains="В плеере доступен короткий демонстрационный фрагмент")
            for movie_id in list(fragments.values_list("pk", flat=True)):
                set_publication(actor, movie_id, False)
    except Exception:
        for key in stored:
            remove_file(key)
        raise


RELEASES = (
    ("bunny", "Большой Бак Банни", 2008, 10, 6, "Комедия",
     "Добродушному кролику надоедают выходки троих лесных задир.",
     "Big Buck Bunny © 2008 Blender Foundation / www.bigbuckbunny.org. CC BY 3.0."),
    ("sintel", "Синтел", 2010, 15, 16, "Фэнтези",
     "Юная путешественница отправляется на поиски дракона, которого когда-то спасла.",
     "Sintel © 2010 Blender Foundation / durian.blender.org. CC BY 3.0."),
)


def import_release(actor, release):
    key, title, year, duration, age, genre_name, description, credit = release
    folder = Path(settings.BASE_DIR) / "private" / "open-films"
    stored = []
    try:
        with transaction.atomic():
            if Movie.objects.filter(title=title, release_year=year).exists():
                return False
            genre, _ = Genre.objects.get_or_create(name=genre_name)
            movie = Movie.objects.create(title=title, release_year=year, duration_min=duration, age_rating=age,
                description=f"{description}\n\n{credit}\nЛицензия: https://creativecommons.org/licenses/by/3.0/\n"
                            "В плеере доступен короткий демонстрационный фрагмент, не полная версия. "
                            "Обложки — кадры из видео. Фрагмент «Синтел» взят из официального трейлера.")
            MovieGenre.objects.create(movie=movie, genre=genre)
            for kind, extension, mime in (("poster", "jpg", "image/jpeg"), ("banner", "jpg", "image/jpeg"),
                                          ("trailer", "mp4", "video/mp4"), ("video", "mp4", "video/mp4")):
                path = folder / f"{key}-{kind}.{extension}"
                with path.open("rb") as source:
                    upload = UploadedFile(source, name=path.name, content_type=mime, size=path.stat().st_size)
                    media = MovieMedia(movie=movie, kind=kind)
                    save_upload(media, upload, validate_upload(upload, kind))
                    stored.append(media.storage_key)
            set_publication(actor, movie.pk, True)
    except Exception:
        for key in stored:
            remove_file(key)
        raise
    return True


def replace_showcase(actor):
    available = [release for release in RELEASES if all(
        (settings.BASE_DIR / "private" / "open-films" / f"{release[0]}-{suffix}").is_file()
        for suffix in ("poster.jpg", "banner.jpg", "trailer.mp4", "video.mp4")
    )]
    if not available:
        raise ValidationError("Нет подготовленных открытых фильмов.")
    from catalog.showcase import FILMS

    added = sum(import_release(actor, release) for release in available)
    marked = Movie.objects.filter(title__in=[film[0] for film in FILMS], is_published=True,
        description__contains="Демонстрационная карточка. Трейлер и полное видео — учебный тестовый ролик.")
    hidden = sum(set_publication(actor, movie_id, False) for movie_id in list(marked.values_list("pk", flat=True)))
    return added, hidden
