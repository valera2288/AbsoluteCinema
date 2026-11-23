from django import forms
from django.contrib import admin, messages
from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import UploadedFile
from django.db import router, transaction
from django.shortcuts import redirect, render
from django.urls import path, reverse
from django.utils.decorators import method_decorator
from django.views.decorators.http import require_http_methods

from catalog.media import remove_file, save_upload, validate_upload
from catalog.models import Genre, Movie, MovieGenre, MovieMedia
from catalog.open_films import publish_defense_catalog, replace_showcase
from catalog.services import set_publication
from catalog.showcase import add_showcase


class GenreForm(forms.ModelForm):
    class Meta:
        model = Genre
        fields = "__all__"

    def clean_name(self):
        return self.cleaned_data["name"].strip()

    def clean_is_active(self):
        active = self.cleaned_data["is_active"]
        if not active and self.instance.pk and Movie.objects.filter(
            genres=self.instance, is_published=True,
        ).exists():
            raise ValidationError("Сначала замените этот жанр в опубликованных фильмах.")
        return active


@admin.register(Genre)
class GenreAdmin(admin.ModelAdmin):
    form = GenreForm
    list_display = ("name", "is_active")

    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        if request.method == "POST" and object_id:
            with transaction.atomic():
                list(Genre.objects.select_for_update().filter(pk=object_id))
                return super().changeform_view(request, object_id, form_url, extra_context)
        return super().changeform_view(request, object_id, form_url, extra_context)


class MovieForm(forms.ModelForm):
    genres = forms.ModelMultipleChoiceField(label="Жанры", queryset=Genre.objects.all(), required=False)

    class Meta:
        model = Movie
        fields = ("title", "release_year", "duration_min", "age_rating", "description", "genres")

    def clean_genres(self):
        genres = self.cleaned_data["genres"]
        locked = list(Genre.objects.select_for_update().filter(pk__in=genres.values("pk")).order_by("pk"))
        if self.instance.is_published and not any(genre.is_active for genre in locked):
            raise ValidationError("В опубликованном фильме нужен хотя бы один действующий жанр.")
        return genres


@admin.register(Movie)
class MovieAdmin(admin.ModelAdmin):
    change_list_template = "admin/catalog/movie/change_list.html"
    form = MovieForm
    list_display = ("title", "release_year", "is_published", "published_at", "publication_link")
    list_filter = ("is_published", "release_year")
    search_fields = ("title",)
    readonly_fields = ("is_published", "published_at", "publication_link")
    actions = None

    def changelist_view(self, request, extra_context=None):
        from django.conf import settings

        return super().changelist_view(request, extra_context={
            **(extra_context or {}), "show_demo_import": settings.DEBUG and request.user.is_superuser,
        })

    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        if request.method == "POST" and object_id:
            with transaction.atomic():
                list(Movie.objects.select_for_update().filter(pk=object_id))
                return super().changeform_view(request, object_id, form_url, extra_context)
        return super().changeform_view(request, object_id, form_url, extra_context)

    def has_delete_permission(self, request, obj=None):
        return bool(obj and not obj.published_at and not obj.media.exists()
                    and super().has_delete_permission(request, obj))

    @transaction.atomic
    def delete_model(self, request, obj):
        movie = Movie.objects.select_for_update().get(pk=obj.pk)
        if not self.has_delete_permission(request, movie):
            raise PermissionDenied
        super().delete_model(request, movie)

    @admin.display(description="Публикация")
    def publication_link(self, obj):
        from django.utils.html import format_html
        if obj and obj.pk:
            return format_html('<a href="{}">{}</a>', reverse("admin:catalog_movie_publication", args=[obj.pk]),
                               "Снять с публикации" if obj.is_published else "Опубликовать")
        return "После сохранения"

    def get_urls(self):
        return [
            path("showcase/", self.admin_site.admin_view(self.showcase), name="catalog_movie_showcase"),
            path("<int:movie_id>/publication/", self.admin_site.admin_view(self.publication),
                 name="catalog_movie_publication"),
        ] + super().get_urls()

    @method_decorator(require_http_methods(["GET", "POST"]))
    def showcase(self, request):
        from django.conf import settings
        from django.http import Http404

        if not settings.DEBUG:
            raise Http404
        if not request.user.is_superuser:
            raise PermissionDenied
        from django.db import connection
        if "reviews" not in connection.introspection.table_names():
            self.message_user(request, "Сначала примените миграцию отзывов.", messages.ERROR)
            return redirect("admin:catalog_movie_changelist")
        if request.method == "POST":
            try:
                if request.POST.get("action") == "defense":
                    publish_defense_catalog(request.user)
                elif request.POST.get("action") == "open_films":
                    added, hidden = replace_showcase(request.user)
                else:
                    created, skipped = add_showcase(request.user)
            except ValidationError as exc:
                self.message_user(request, " ".join(exc.messages), messages.ERROR)
            except OSError:
                self.message_user(request, "Не удалось прочитать подготовленные файлы или сохранить медиа.", messages.ERROR)
            else:
                if request.POST.get("action") == "defense":
                    self.message_user(request, "Полная версия и ролик для скачивания опубликованы. Карточки фрагментов сохранены как черновики.")
                elif request.POST.get("action") == "open_films":
                    self.message_user(request, f"Открытых фильмов добавлено: {added}. Учебных карточек скрыто: {hidden}.")
                else:
                    self.message_user(request, f"Добавлено: {len(created)}. Пропущено совпадений: {len(skipped)}.")
            return redirect("admin:catalog_movie_changelist")
        return render(request, "admin/catalog/movie/showcase.html", {
            **self.admin_site.each_context(request), "title": "Наполнение учебной витрины",
        })

    @method_decorator(require_http_methods(["GET", "POST"]))
    def publication(self, request, movie_id):
        from django.shortcuts import get_object_or_404
        if not request.user.has_perm("catalog.publish_movie"):
            raise PermissionDenied
        movie = get_object_or_404(Movie, pk=movie_id)
        error = None
        if request.method == "POST":
            action = request.POST.get("action")
            if action not in ("publish", "unpublish"):
                return redirect("admin:catalog_movie_publication", movie_id=movie.pk)
            try:
                set_publication(request.user, movie_id, action == "publish")
            except ValidationError as exc:
                error = " ".join(exc.messages)
            else:
                messages.success(request, "Состояние публикации сохранено.")
                return redirect("admin:catalog_movie_changelist")
        return render(request, "admin/movie_publication.html", {
            **admin.site.each_context(request), "movie": movie, "error": error,
            "title": "Публикация фильма",
        }, status=400 if error else 200)


class MediaForm(forms.ModelForm):
    upload = forms.FileField(label="Файл", required=False)

    class Meta:
        model = MovieMedia
        fields = ("movie", "kind", "variant", "allow_download", "upload")

    def clean(self):
        data = super().clean()
        movie, kind, upload = (data.get(field) for field in ("movie", "kind", "upload"))
        if self.instance.pk:
            movie = self.instance.movie
            kind = self.instance.kind
        if not upload and not self.instance.pk:
            self.add_error("upload", "Выберите файл.")
        if movie is not None and kind and upload:
            try:
                self.metadata = validate_upload(upload, kind)
                if kind == "video":
                    other = movie.media.filter(kind="video").exclude(pk=self.instance.pk).first()
                    baseline = other or (self.instance if movie.published_at else None)
                    if baseline and abs(baseline.duration - self.metadata[2]) > 1:
                        raise ValidationError("Длительность вариантов одного фильма должна совпадать с точностью до секунды.")
            except (ValidationError, OSError) as exc:
                self.add_error("upload", exc if isinstance(exc, ValidationError) else "Не удалось прочитать файл.")
        return data


@admin.register(MovieMedia)
class MediaAdmin(admin.ModelAdmin):
    form = MediaForm
    list_display = ("movie", "kind", "variant", "file_format", "size", "duration")
    list_filter = ("kind",)
    actions = ("install_official_posters",)

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        if not settings.DEBUG or not request.user.is_superuser:
            actions.pop("install_official_posters", None)
        return actions

    @admin.action(description="Заменить выбранные постеры на официальные", permissions=["change"])
    def install_official_posters(self, request, queryset):
        if not settings.DEBUG or not request.user.is_superuser:
            raise PermissionDenied
        files = {("Большой Бак Банни", 2008): "bunny.jpg", ("Синтел", 2010): "sintel.jpg"}
        count = 0
        for item in queryset.filter(kind="poster").select_related("movie").order_by("pk"):
            name = files.get((item.movie.title, item.movie.release_year))
            if not name:
                continue
            path = settings.BASE_DIR / "private" / "official-posters" / name
            try:
                with path.open("rb") as source:
                    upload = UploadedFile(source, name=name, content_type="image/jpeg", size=path.stat().st_size)
                    metadata = validate_upload(upload, "poster")
                    with transaction.atomic():
                        Movie.objects.select_for_update().get(pk=item.movie_id)
                        media = MovieMedia.objects.get(pk=item.pk, kind="poster")
                        save_upload(media, upload, metadata)
                        self.log_change(request, media, "Установлена оригинальная афиша Blender Foundation.")
            except (ValidationError, OSError, MovieMedia.DoesNotExist):
                self.message_user(request, f"Не удалось заменить постер: {item.movie.title}.", messages.ERROR)
            else:
                count += 1
        self.message_user(request, f"Обновлено постеров: {count}.")

    def get_readonly_fields(self, request, obj=None):
        return ("movie", "kind", "variant") if obj else ()

    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        if request.method == "POST":
            with transaction.atomic(using=router.db_for_write(MovieMedia)):
                if object_id:
                    movie_ids = MovieMedia.objects.filter(pk=object_id).values("movie_id")
                    list(Movie.objects.select_for_update().filter(pk__in=movie_ids))
                else:
                    movie_id = request.POST.get("movie", "")
                    if movie_id.isdecimal() and len(movie_id) <= 18:
                        list(Movie.objects.select_for_update().filter(pk=movie_id))
                return super().changeform_view(request, object_id, form_url, extra_context)
        return super().changeform_view(request, object_id, form_url, extra_context)

    def save_model(self, request, obj, form, change):
        upload = form.cleaned_data.get("upload")
        if upload:
            save_upload(obj, upload, form.metadata)
        else:
            super().save_model(request, obj, form, change)

    def has_delete_permission(self, request, obj=None):
        return bool(obj and not obj.movie.is_published and super().has_delete_permission(request, obj))

    @transaction.atomic
    def delete_model(self, request, obj):
        movie = Movie.objects.select_for_update().get(pk=obj.movie_id)
        if movie.is_published:
            raise PermissionDenied
        obj = MovieMedia.objects.get(pk=obj.pk)
        key = obj.storage_key
        super().delete_model(request, obj)
        transaction.on_commit(lambda: remove_file(key))
