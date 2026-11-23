from django.db import models
from django.db.models import F, Q


class PlaybackSession(models.Model):
    id = models.UUIDField(primary_key=True, editable=False)
    tab_key = models.UUIDField(editable=False)
    profile = models.ForeignKey("accounts.Profile", on_delete=models.CASCADE)
    movie = models.ForeignKey("catalog.Movie", on_delete=models.PROTECT)
    device = models.ForeignKey("accounts.Device", on_delete=models.CASCADE)
    subscription = models.ForeignKey("billing.Subscription", on_delete=models.PROTECT)
    started_at = models.DateTimeField()
    last_seen_at = models.DateTimeField()
    closed_at = models.DateTimeField(null=True)
    close_reason = models.CharField(max_length=24, blank=True)
    last_update_seq = models.PositiveBigIntegerField(default=0)

    class Meta:
        db_table = "playback_sessions"
        indexes = [
            models.Index(fields=["subscription", "last_seen_at"], condition=Q(closed_at__isnull=True), name="playback_live_period"),
            models.Index(fields=["device", "closed_at"], name="playback_device_closed"),
        ]


class WatchHistory(models.Model):
    profile = models.ForeignKey("accounts.Profile", on_delete=models.CASCADE)
    movie = models.ForeignKey("catalog.Movie", on_delete=models.CASCADE)
    writer_session = models.ForeignKey(PlaybackSession, null=True, on_delete=models.SET_NULL)
    progress_sec = models.PositiveIntegerField(default=0)
    watched_at = models.DateTimeField()
    is_completed = models.BooleanField(default=False)

    class Meta:
        db_table = "watch_history"
        constraints = [models.UniqueConstraint(fields=["profile", "movie"], name="watch_history_profile_movie")]
        indexes = [models.Index(fields=["profile", "-watched_at"], name="watch_history_recent")]


class WatchEvent(models.Model):
    movie = models.ForeignKey("catalog.Movie", on_delete=models.PROTECT)
    profile = models.ForeignKey("accounts.Profile", null=True, on_delete=models.SET_NULL)
    session = models.OneToOneField(PlaybackSession, null=True, on_delete=models.SET_NULL)
    started_at = models.DateTimeField()
    completed_at = models.DateTimeField(null=True)

    class Meta:
        db_table = "watch_events"
        constraints = [models.CheckConstraint(condition=Q(completed_at__isnull=True) | Q(completed_at__gte=F("started_at")), name="watch_event_completion_date")]
        indexes = [
            models.Index(fields=["started_at", "movie"], name="watch_event_starts"),
            models.Index(fields=["completed_at", "movie"], condition=Q(completed_at__isnull=False), name="watch_event_completions"),
        ]
