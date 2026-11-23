import os

from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import FileResponse, Http404, HttpResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe

from billing.models import Tariff
from catalog.forms import CatalogFilter
from catalog.media import MIME_TYPES, media_path
from catalog.models import Favorite, MovieMedia, Review
from catalog.queries import PUBLIC_KINDS, filtered_movies, prepare_movie, published_movies, visible_movies
from config.media_probe import RANGE, range_number
from billing.services import current_subscription


@never_cache
def home(request):
    movies = [prepare_movie(movie) for movie in published_movies(request.viewer_device)[:6]]
    return render(request, "home.html", {"featured": movies[0] if movies else None, "movies": movies})


@never_cache
def catalog(request):
    form = CatalogFilter(request.GET)
    page = None
    links = []
    if form.is_valid():
        page = Paginator(filtered_movies(form.cleaned_data, request.viewer_device), 12).get_page(request.GET.get("page", 1))
        for movie in page.object_list:
            prepare_movie(movie)
        for label, number in (("Назад", page.number - 1), ("Далее", page.number + 1)):
            if 1 <= number <= page.paginator.num_pages:
                parameters = request.GET.copy()
                parameters["page"] = number
                links.append((label, parameters.urlencode()))
    return render(request, "catalog/list.html", {"form": form, "page": page, "links": links})


@never_cache
def detail(request, movie_id):
    movie = prepare_movie(get_object_or_404(published_movies(request.viewer_device), pk=movie_id))
    device = request.viewer_device
    entitled = bool(device and device.active_profile_id and current_subscription(device.user_id))
    profile_id = device.active_profile_id if device else None
    public_reviews = movie.reviews.filter(moderation_status=Review.Status.PUBLISHED).select_related("profile").order_by("-submitted_at", "-pk")
    review_page = Paginator(public_reviews, 12).get_page(request.GET.get("reviews_page"))
    return render(request, "catalog/detail.html", {
        "movie": movie, "genres": movie.genres.filter(is_active=True),
        "has_video": movie.media.filter(kind="video").exists(), "entitled": entitled,
        "downloads": movie.media.filter(kind="video", allow_download=True) if entitled else (),
        "favorited": bool(profile_id and Favorite.objects.filter(profile_id=profile_id, movie=movie).exists()),
        "own_review": Review.objects.filter(profile_id=profile_id, movie=movie).first() if profile_id else None,
        "review_page": review_page,
    })


def tariffs(request):
    return render(request, "catalog/tariffs.html", {"tariffs": Tariff.objects.filter(is_active=True)})


class TrailerRange:
    def __init__(self, source, start, length):
        self.source = source
        self.start = start
        self.length = length

    def __iter__(self):
        self.source.seek(self.start)
        remaining = self.length
        while remaining and (chunk := self.source.read(min(remaining, 65536))):
            remaining -= len(chunk)
            yield chunk

    def close(self):
        self.source.close()


@never_cache
@require_safe
def public_media(request, movie_id, media_id, version):
    material = get_object_or_404(
        MovieMedia, pk=media_id, movie_id=movie_id, kind__in=PUBLIC_KINDS,
        movie__in=visible_movies(request.viewer_device),
    )
    if material.version_id != version:
        return HttpResponse(status=410)
    try:
        source = media_path(material.storage_key).open("rb")
    except (OSError, ValidationError):
        raise Http404 from None
    if os.fstat(source.fileno()).st_size != material.size:
        source.close()
        raise Http404
    mime = MIME_TYPES[material.file_format]
    size = material.size
    headers = {"Content-Length": str(size), "X-Content-Type-Options": "nosniff"}
    if material.kind == "trailer":
        headers["Accept-Ranges"] = "bytes"
    if request.method == "HEAD":
        source.close()
        return HttpResponse(content_type=mime, headers=headers)
    header = request.headers.get("Range")
    if material.kind != "trailer" or header is None:
        return FileResponse(source, content_type=mime, headers=headers)
    match = RANGE.fullmatch(header.strip())
    if not match or not any(match.groups()):
        source.close()
        return HttpResponse(status=400)
    first, last = match.groups()
    start, end = 0, size - 1
    if first:
        start = range_number(first, size)
        if last:
            end = min(end, range_number(last, size))
    else:
        start = max(0, size - range_number(last, size))
    if start > end:
        source.close()
        return HttpResponse(status=416, headers={"Content-Range": f"bytes */{size}"})
    length = end - start + 1
    headers.update({"Content-Range": f"bytes {start}-{end}/{size}", "Content-Length": str(length)})
    return StreamingHttpResponse(TrailerRange(source, start, length), status=206, content_type=mime, headers=headers)
