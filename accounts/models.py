from django.contrib.auth.base_user import BaseUserManager
from django.contrib.auth.models import AbstractUser
from django.core.validators import RegexValidator
from django.db import models
from django.db.models.functions import Lower


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create_user(self, login, email, password, **extra_fields):
        user = self.model(login=login, email=email, **extra_fields)
        user.set_password(password)
        user.full_clean(validate_unique=False, validate_constraints=False)
        user.save(using=self._db)
        return user

    def create_user(self, login, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(login, email, password, **extra_fields)

    def create_superuser(self, login, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        if extra_fields["is_staff"] is not True or extra_fields["is_superuser"] is not True:
            raise ValueError("Superuser requires is_staff=True and is_superuser=True.")
        return self._create_user(login, email, password, **extra_fields)


class User(AbstractUser):
    username = None
    first_name = None
    last_name = None
    login = models.CharField(
        "логин", max_length=30, unique=True,
        validators=[RegexValidator(
            r"\A[A-Za-z][A-Za-z0-9_]{3,29}\Z",
            "Укажите от 4 до 30 латинских букв, цифр или знаков подчёркивания; начните с буквы.",
        )],
    )
    email = models.EmailField("электронная почта", max_length=254)
    full_name = models.CharField("имя", max_length=150, blank=True)
    phone = models.CharField(
        "телефон", max_length=16, blank=True,
        validators=[RegexValidator(r"\A\+?[0-9]{10,15}\Z", "Укажите 10–15 цифр; в начале допустим «+».")],
    )
    can_post_reviews = models.BooleanField("может писать отзывы", default=True)
    pin_failed_attempts = models.PositiveSmallIntegerField(default=0, editable=False)
    pin_locked_until = models.DateTimeField(null=True, blank=True, editable=False)

    USERNAME_FIELD = "login"
    REQUIRED_FIELDS = ["email"]
    objects = UserManager()

    class Meta:
        db_table = "users"
        verbose_name = "пользователь"
        verbose_name_plural = "пользователи"
        constraints = [
            models.UniqueConstraint(Lower("login"), name="unique_lower_login"),
            models.UniqueConstraint(Lower("email"), name="unique_lower_email"),
        ]
        permissions = [
            ("restrict_review_authors", "Может ограничивать право автора писать отзывы"),
            ("view_media_probe", "Может смотреть пробное видео"),
        ]

    def clean_fields(self, exclude=None):
        excluded = exclude or ()
        if "login" not in excluded:
            self.login = (self.login or "").strip().lower()
        if "email" not in excluded:
            self.email = (self.email or "").strip().lower()
        super().clean_fields(exclude=exclude)

    def get_full_name(self):
        return self.full_name

    def get_short_name(self):
        return self.full_name or self.login


class Profile(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="profiles")
    profile_name = models.CharField("Имя профиля", max_length=50)
    is_kids = models.BooleanField("Детский профиль", default=False)
    pin_hash = models.CharField(max_length=128, blank=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "profiles"
        ordering = ("created_at", "pk")


class Device(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="devices")
    session_key = models.CharField(max_length=40, unique=True, editable=False)
    name = models.CharField(max_length=40)
    device_type = models.CharField(max_length=20)
    active_profile = models.ForeignKey(Profile, null=True, on_delete=models.SET_NULL, related_name="+")
    guarded_profile = models.ForeignKey(Profile, null=True, on_delete=models.SET_NULL, related_name="+")
    kids_locked = models.BooleanField(default=False)
    last_login = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now_add=True)
    revoked_at = models.DateTimeField(null=True)

    class Meta:
        db_table = "devices"
        indexes = [models.Index(fields=["user", "-last_seen_at"], name="devices_recent")]


class LoginThrottle(models.Model):
    scope = models.CharField(max_length=8)
    subject_key = models.CharField(max_length=64, blank=True)
    ip_key = models.CharField(max_length=64)
    attempts = models.JSONField(default=list)
    locked_until = models.DateTimeField(null=True)
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
        db_table = "login_throttles"
        constraints = [models.UniqueConstraint(fields=["scope", "subject_key", "ip_key"], name="unique_login_throttle")]
