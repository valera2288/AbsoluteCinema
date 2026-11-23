from django.urls import path

from viewing import views

urlpatterns = [
    path("history/", views.history, name="watch-history"),
    path("movie/<int:movie_id>/", views.player, name="watch-player"),
    path("movie/<int:movie_id>/intent/", views.intent, name="watch-intent"),
    path("movie/<int:movie_id>/download/<int:media_id>/", views.download, name="watch-download"),
    path("start/", views.start, name="watch-start"),
    path("sessions/<uuid:session_id>/activity/", views.activity, name="watch-activity"),
    path("sessions/<uuid:session_id>/refresh/", views.refresh, name="watch-refresh"),
    path("sessions/<uuid:session_id>/media/<int:media_id>/<uuid:version>/", views.media, name="watch-media"),
]
