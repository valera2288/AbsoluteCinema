from io import BytesIO
from math import sin
from pathlib import Path

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.utils import timezone
from PIL import Image, ImageDraw, ImageFont

from accounts.models import User
from catalog.media import remove_file, save_upload, validate_upload
from catalog.models import Genre, Movie, MovieGenre, MovieMedia, Review
from catalog.reviews import decide
from catalog.services import set_publication

FILMS = (
    ("Ночной сигнал", "NIGHT SIGNAL", "Фантастика", 12, (80, 46, 135),
     "Оператор ночной станции ловит передачу с позывным, который исчез из журнала двадцать лет назад.",
     "Хорошо передано ощущение ночного эфира. Световые импульсы работают лучше любого объяснения.", 5),
    ("Последний трамвай", "LAST TRAM", "Драма", 6, (144, 79, 48),
     "На последнем рейсе водитель находит записку пассажира и меняет маршрут, чтобы вернуть её адресату.",
     "Тёплые огни и неторопливый ритм хорошо передают настроение позднего города.", 4),
    ("Тропа над облаками", "ABOVE THE CLOUDS", "Приключения", 0, (40, 117, 116),
     "Двое друзей поднимаются к старой обсерватории и встречают рассвет выше облаков.",
     "Понравилось, как из простых линий возникает ощущение высоты и открытого пространства.", 5),
    ("Архив дождя", "RAIN ARCHIVE", "Детектив", 12, (49, 88, 142),
     "Архивист находит запись ливня, которого никто в городе не помнит.",
     "Хорошая завязка для маленького городского детектива.", 4),
    ("Дом с маяком", "LIGHTHOUSE", "Приключения", 6, (138, 75, 103),
     "Друзья пытаются зажечь старый маяк перед возвращением рыбацкой лодки.",
     "Уютная история, особенно понравилась смена света в финале.", 5),
    ("Линия прилива", "TIDE LINE", "Драма", 16, (61, 106, 92),
     "Два брата встречаются на берегу, где когда-то решили больше не разговаривать.",
     "Запомнилась сдержанная атмосфера и работа с цветом.", 4),
)
DEMO_LOGIN = "cinema_showcase"


def artwork(label, color, size, poster):
    width, height = size
    canvas = Image.new("RGB", size)
    draw = ImageDraw.Draw(canvas)
    for row in range(height):
        strength = .16 + .78 * (1 - abs(row / height - .45))
        draw.line((0, row, width, row), fill=tuple(int(13 + channel * strength) for channel in color))
    center_x, center_y = int(width * (.5 if poster else .72)), int(height * .39)
    for radius in range(22, int(height * .28), 19):
        draw.ellipse((center_x-radius, center_y-radius, center_x+radius, center_y+radius),
                     outline="#e1ded7", width=1)
    for layer in range(3):
        points = [(offset, int(height * (.56 + layer * .065)) + int(sin(offset / (28 + layer * 13)) * height * .043))
                  for offset in range(width)]
        draw.line(points, fill=("#f1cab5", "#141927", "#d1b8df")[layer], width=3)
    if poster:
        draw.text((width/2, height*.78), label, anchor="mm", fill="#fff6ec",
                  font=ImageFont.load_default(size=38))
        draw.text((width/2, height*.86), "ABSOLUTE CINEMA", anchor="mm", fill="#e0d7e9",
                  font=ImageFont.load_default(size=19))
    output = BytesIO()
    canvas.save(output, format="JPEG", quality=89)
    return output.getvalue()


def add_showcase(actor):
    if not actor.is_superuser or not actor.is_active or not actor.is_staff:
        raise PermissionDenied
    source = Path(settings.BASE_DIR) / "private" / "online-cinema-probe.mp4"
    if not source.is_file():
        raise ValidationError("Не найден исходный демонстрационный MP4.")
    payload = source.read_bytes()
    prepared = []
    for kind in ("trailer", "video"):
        upload = SimpleUploadedFile(f"{kind}.mp4", payload, content_type="video/mp4")
        prepared.append((kind, upload, validate_upload(upload, kind)))
    created, skipped = [], []
    for title, label, genre_name, age, color, description, text, rating in FILMS:
        keys = []
        try:
            with transaction.atomic():
                User.objects.select_for_update().get(pk=actor.pk)
                if Movie.objects.filter(title=title).exists():
                    skipped.append(title)
                    continue
                author, _ = User.objects.get_or_create(login=DEMO_LOGIN, defaults={
                    "email": "cinema-showcase@example.test", "password": "!", "is_active": False,
                })
                if author.is_active or author.has_usable_password() or author.email != "cinema-showcase@example.test":
                    raise ValidationError("Имя демонстрационного автора занято другим аккаунтом.")
                profile, _ = author.profiles.get_or_create(profile_name="Демонстрационный отзыв")
                genre = Genre.objects.filter(name__iexact=genre_name, is_active=True).first()
                if genre is None:
                    if Genre.objects.filter(name__iexact=genre_name).exists():
                        raise ValidationError(f"Жанр «{genre_name}» отключён — его настройки не изменены.")
                    genre = Genre.objects.create(name=genre_name)
                movie = Movie.objects.create(title=title, release_year=2026, duration_min=2, age_rating=age,
                    description=f"{description}\n\nДемонстрационная карточка. Трейлер и полное видео — учебный тестовый ролик.")
                MovieGenre.objects.create(movie=movie, genre=genre)
                images = []
                for kind, size in (("poster", (600, 900)), ("banner", (1280, 640))):
                    upload = SimpleUploadedFile(f"{kind}.jpg", artwork(label, color, size, kind == "poster"),
                                                content_type="image/jpeg")
                    images.append((kind, upload, validate_upload(upload, kind)))
                for kind, upload, metadata in [*images, *prepared]:
                    media = MovieMedia(movie=movie, kind=kind)
                    save_upload(media, upload, metadata)
                    keys.append(media.storage_key)
                set_publication(actor, movie.pk, True)
                review = Review.objects.create(profile=profile, movie=movie, rating=rating, text=text,
                                               moderation_status=Review.Status.PENDING, submitted_at=timezone.now())
                decide(actor.pk, review.pk, "publish", review.version)
        except Exception:
            for key in keys:
                remove_file(key)
            raise
        created.append(title)
    return created, skipped
