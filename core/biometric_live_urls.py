from django.urls import path
from . import biometric_live_views as live

urlpatterns = [
    path("config/", live.live_config, name="biometric_live_config"),
    path("events/", live.live_events, name="biometric_live_events"),
]
