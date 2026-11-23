from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from django.contrib.auth.models import Group
from django.db import transaction
from django.utils import timezone

from accounts.models import User
from billing import services as billing
from billing.models import PromoCode, Tariff
from catalog.models import Favorite, Movie, Review
from catalog.reviews import decide
from operations.models import AuditEntry
from operations.services import create_request, handle_request, set_marketing
from viewing.models import WatchEvent, WatchHistory


PEOPLE = (
    ("lena", "Елена Рощина", "paid", "published", "Субтитры в плеере", "Есть ли субтитры у учебных фильмов?", "close"),
    ("maksim", "Максим Ладин", "refunded", "published", "Возврат подписки", "Хочу уточнить статус тестового возврата.", "close"),
    ("nina", "Нина Озерова", "free", "pending", "Детский профиль", "Как переключить ограничение на взрослом профиле?", "work"),
    ("anton", "Антон Мельников", "declined", "rejected", "Повтор оплаты", "Первая учебная попытка не прошла. Можно повторить?", "work"),
    ("sofia", "София Ветрова", "pending", "", "Выбор тарифа", "Подскажите, сколько экранов входит в семейный тариф.", ""),
    ("daria", "Дарья Белова", "paid", "published", "Продолжить просмотр", "Где находится история просмотренных фильмов?", ""),
)
OPINIONS = (
    "Понравился неспешный ритм и то, как настроение меняется со светом.",
    "Удачная короткая зарисовка. Особенно запомнилась последняя сцена.",
    "Интересная идея, но хотелось бы чуть подробнее раскрыть финал.",
    "Мне не подошёл темп: завязка интересная, но хотелось больше деталей.",
)


@transaction.atomic
def populate(actor):
    User.objects.select_for_update().get(pk=actor.pk)
    viewers = Group.objects.get_or_create(name="Учебные зрители")[0]
    tariffs = [
        Tariff.objects.get_or_create(name=name, defaults={"price": Decimal(price), "period_days": days,
            "max_streams": streams, "description": description, "is_active": active})[0]
        for name, price, days, streams, description, active in (
            ("Демо · Один экран", "199.00", 30, 1, "Для просмотра на одном устройстве.", True),
            ("Демо · Семейный", "429.00", 30, 3, "До трёх одновременных просмотров.", True),
            ("Демо · Архивный", "149.00", 7, 1, "Пример снятого с продажи тарифа.", False),
        )
    ]
    today = timezone.localdate()
    for code, percent, start, end in (
        ("SHOWCASE100", 100, today, today + timedelta(days=90)),
        ("SHOWCASE20", 20, today, today + timedelta(days=90)),
        ("SHOWCASE-OLD", 15, today - timedelta(days=60), today - timedelta(days=1)),
    ):
        if not PromoCode.objects.filter(code__iexact=code).exists():
            PromoCode.objects.create(code=code, discount_percent=percent, valid_from=start, valid_to=end)
    movies = list(Movie.objects.filter(is_published=True).order_by("pk")[:6])
    count = 0
    for index, (key, name, outcome, moderation, subject, body, answer) in enumerate(PEOPLE):
        login = f"demo_{key}"
        email = f"{login}@example.test"
        if User.objects.filter(login__iexact=login).exists() or User.objects.filter(email__iexact=email).exists():
            continue
        user = User.objects.create_user(login, email, full_name=name)
        user.groups.add(viewers)
        profile = user.profiles.create(profile_name=f"{name.split()[0]} · демо")
        if index in (1, 2):
            user.profiles.create(profile_name="Детский · демо", is_kids=True)
        AuditEntry.objects.create(actor=actor, subject_user=user, action="account.roles",
                                  object_kind="account", object_ref="", result_code="ok")
        tariff = tariffs[index % 2]
        promo = "SHOWCASE100" if outcome == "free" else "SHOWCASE20" if index == 0 else ""
        token = billing.checkout_token(user, billing.quote(tariff, promo, timezone.now()), promo, index == 0)
        payment = billing.checkout(user.pk, token)
        if outcome in ("paid", "refunded", "declined"):
            payment = billing.confirm_payment(user.pk, payment.operation_id, outcome != "declined")
        if outcome == "refunded":
            billing.refund_payment(actor, payment.operation_id)
        set_marketing(user.pk, index % 2 == 0)
        ticket = create_request(user.pk, "Демо · " + subject, body)
        if answer:
            handle_request(actor.pk, ticket.pk, answer, reply_code="resolved" if answer == "close" else "reviewed",
                           body="Это обращение из учебного набора.", request_key=uuid4())
        if movies:
            movie = movies[index % len(movies)]
            Favorite.objects.create(profile=profile, movie=movie)
            if moderation:
                now = timezone.now()
                review = Review.objects.create(profile=profile, movie=movie, rating=3 if moderation == "rejected" else 4 + index % 2,
                    text=OPINIONS[index % len(OPINIONS)], moderation_status="pending", submitted_at=now)
                if moderation in ("published", "rejected"):
                    decide(actor.pk, review.pk, "publish" if moderation == "published" else "reject", review.version,
                           reason="Учебная пометка: отзыв отправлен на доработку.")
            media = movie.media.filter(kind="video", duration__gte=2).first()
            if media and outcome in ("paid", "free", "refunded"):
                now = timezone.now()
                completed = index % 2 == 0
                WatchHistory.objects.create(profile=profile, movie=movie, watched_at=now,
                    progress_sec=int(media.duration) if completed else max(1, int(media.duration / 3)), is_completed=completed)
                WatchEvent.objects.create(movie=movie, profile=profile, started_at=now - timedelta(minutes=2),
                                          completed_at=now if completed else None)
        count += 1
    return count
