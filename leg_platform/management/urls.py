"""URLs of the management area, under ``/management/`` (D-077).

Every route needs a view that carries the access check (``leg_platform.management.access``); a
test walks this module and fails for a route without it. URL names of an area start with its menu
key (``navigation.MENU``), so that the menu marks the entry as active.
"""

from django.urls import path

from leg_platform.management import views

app_name = "management"

urlpatterns = [
    path("", views.OverviewView.as_view(), name="overview"),
    path("settings/", views.SettingsView.as_view(), name="settings"),
]
