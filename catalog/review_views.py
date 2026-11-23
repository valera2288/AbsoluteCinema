from django import forms
from django.contrib import messages
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST, require_http_methods, require_safe

from accounts.views import viewer_required
from catalog import reviews
from catalog.forms import RatingForm, ReviewForm, ReviewQueueFilter
from catalog.models import Favorite, Review
from catalog.queries import prepare_movie, published_movies, visible_movies
from viewing.services import PlaybackError


class VersionForm(forms.Form):
    version = forms.IntegerField(min_value=0, widget=forms.HiddenInput)


class DecisionForm(VersionForm):
    reason = forms.CharField(label="Причина", min_length=5, max_length=500, widget=forms.Textarea(attrs={"rows": 3}))


class ClearReviewForm(VersionForm):
    profile_id = forms.IntegerField(min_value=1, widget=forms.HiddenInput)


@viewer_required
@require_POST
def favorite(request, movie_id, action):
    if action not in ("add", "remove"):
        raise Http404
    try:
        reviews.favorite(request, movie_id, action == "add")
    except PlaybackError as exc:
        raise PermissionDenied(exc.message) from exc
    return redirect("movie-detail", movie_id=movie_id)


@viewer_required
@require_http_methods(["GET", "POST"])
def edit(request, movie_id, action):
    forms_by_action = {"rate": (RatingForm, "Оценить фильм"), "text": (ReviewForm, "Написать отзыв"),
                       "clear": (ClearReviewForm, "Удалить текст отзыва")}
    if action not in forms_by_action:
        raise Http404
    if not request.viewer_device.active_profile_id:
        return redirect("viewer-dashboard")
    if action == "text" and not request.user.can_post_reviews:
        raise PermissionDenied
    movie = get_object_or_404(visible_movies(request.viewer_device), pk=movie_id)
    review = Review.objects.filter(profile_id=request.viewer_device.active_profile_id, movie=movie).first()
    form_class, title = forms_by_action[action]
    form = form_class(request.POST if request.method == "POST" else None,
                      initial={"profile_id": request.viewer_device.active_profile_id,
                               "version": review.version if review else 0, "rating": review.rating if review else None,
                               "text": review.text if review else ""})
    if request.method == "POST" and form.is_valid():
        try:
            reviews.save_review(request, movie_id, action, **form.cleaned_data)
        except ValidationError as exc:
            form.add_error(None, exc)
        except PlaybackError as exc:
            raise PermissionDenied(exc.message) from exc
        else:
            messages.success(request, "Отзыв отправлен на модерацию." if action == "text" else "Изменения сохранены.")
            return redirect("movie-detail", movie_id=movie_id)
    return render(request, "catalog/review_form.html", {"title": title, "movie": movie, "form": form})


def paginate(request, rows, size=20):
    page = Paginator(rows, size).get_page(request.GET.get("page"))
    links = []
    for label, number in (("Назад", page.number - 1), ("Далее", page.number + 1)):
        if 1 <= number <= page.paginator.num_pages:
            query = request.GET.copy()
            query["page"] = number
            links.append((label, query.urlencode()))
    return page, links


@viewer_required
@require_safe
def collection(request, section):
    profile_id = request.viewer_device.active_profile_id
    if section == "favorites":
        ids = Favorite.objects.filter(profile_id=profile_id, profile__user=request.user).values("movie_id")
        movies = published_movies(request.viewer_device).filter(pk__in=ids)
        page, links = paginate(request, movies)
        return render(request, "catalog/review_collection.html", {"title": "Избранное", "page": page, "links": links,
            "movies": [prepare_movie(movie) for movie in page.object_list]})
    if section != "reviews":
        raise Http404
    rows = Review.objects.filter(profile_id=profile_id, profile__user=request.user,
                                 movie__in=visible_movies(request.viewer_device)).select_related("movie").order_by("-updated_at", "-pk")
    page, links = paginate(request, rows)
    return render(request, "catalog/review_collection.html", {"title": "Мои оценки и отзывы", "page": page, "links": links,
                                                              "author_reviews": page.object_list})


@never_cache
@require_safe
def queue(request):
    if not request.user.is_authenticated:
        return redirect_to_login(request.get_full_path())
    if not request.user.has_perm("catalog.view_review_queue"):
        raise PermissionDenied
    data = request.GET.copy()
    data.setdefault("status", Review.Status.PENDING)
    form = ReviewQueueFilter(data)
    rows = Review.objects.none()
    if form.is_valid():
        values = form.cleaned_data
        rows = Review.objects.filter(moderation_status=values["status"]).select_related("profile__user", "movie")
        if values["date_from"]:
            rows = rows.filter(submitted_at__date__gte=values["date_from"])
        if values["date_to"]:
            rows = rows.filter(submitted_at__date__lte=values["date_to"])
        rows = rows.order_by("submitted_at", "pk")
    page, links = paginate(request, rows)
    return render(request, "catalog/moderation_queue.html", {"form": form, "page": page, "links": links})


@never_cache
@require_http_methods(["GET", "POST"])
def decision(request, review_id, action):
    if action not in reviews.DECISIONS:
        raise Http404
    permission, expected, _ = reviews.DECISIONS[action]
    if not request.user.has_perm("catalog.view_review_queue") or not request.user.has_perm(permission):
        raise PermissionDenied
    review = get_object_or_404(Review.objects.select_related("movie", "profile"), pk=review_id)
    form_class = VersionForm if action == "publish" else DecisionForm
    form = form_class(request.POST if request.method == "POST" else None, initial={"version": review.version})
    if request.method == "POST" and form.is_valid():
        try:
            reviews.decide(request.user.pk, review_id, action, **form.cleaned_data)
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            messages.success(request, "Решение сохранено.")
            return redirect("moderation-queue")
    titles = {"publish": "Опубликовать отзыв", "reject": "Отклонить отзыв", "remove": "Снять отзыв с публикации"}
    return render(request, "catalog/moderation_decision.html", {"review": review, "form": form, "title": titles[action],
                                                               "can_decide": review.moderation_status == expected})


@require_POST
def restriction(request, review_id):
    if not request.user.has_perm("catalog.view_review_queue") or not request.user.has_perm("accounts.restrict_review_authors"):
        raise PermissionDenied
    action = request.POST.get("action")
    if action not in ("allow", "restrict"):
        raise Http404
    reviews.restrict_author(request.user.pk, review_id, allow=action == "allow", expected=action == "restrict")
    return redirect("moderation-queue")
