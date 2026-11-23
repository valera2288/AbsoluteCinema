from django.conf import settings
from django.contrib import admin
from django.urls import include, path
from django.views.generic import TemplateView

from catalog import review_views, views
from viewing import views as viewing_views

admin.site.site_header = f"{settings.SITE_NAME} — управление"
admin.site.site_title = settings.SITE_NAME

urlpatterns = [
    path("account/", include("operations.urls")),
    path("account/collection/<str:section>/", review_views.collection, name="viewer-collection"),
    path("account/recommendations/", viewing_views.recommendation_feed, name="viewer-recommendations"),
    path("movies/<int:movie_id>/favorite/<str:action>/", review_views.favorite, name="movie-favorite"),
    path("movies/<int:movie_id>/review/<str:action>/", review_views.edit, name="movie-review"),
    path("moderation/reviews/", review_views.queue, name="moderation-queue"),
    path("moderation/reviews/<int:review_id>/author/", review_views.restriction, name="moderation-author"),
    path("moderation/reviews/<int:review_id>/<str:action>/", review_views.decision, name="moderation-decision"),
    path("account/", include("accounts.urls")),
    path("account/billing/", include("billing.urls")),
    path("watch/", include("viewing.urls")),
    path("", views.home, name="home"),
    path("catalog/", views.catalog, name="catalog"),
    path("movies/<int:movie_id>/", views.detail, name="movie-detail"),
    path("movies/<int:movie_id>/media/<int:media_id>/<uuid:version>/", views.public_media, name="movie-media"),
    path("tariffs/", views.tariffs, name="tariffs"),
    path("contacts/", TemplateView.as_view(template_name="catalog/contacts.html"), name="contacts"),
    path("rules/", TemplateView.as_view(template_name="catalog/rules.html"), name="rules"),
    path("admin/", admin.site.urls),
]

if settings.MEDIA_PROBE_ENABLED:
    from config.probe_urls import urlpatterns as probe_patterns

    urlpatterns += probe_patterns
