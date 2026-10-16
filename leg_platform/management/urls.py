"""URLs of the management area, under ``/management/`` (D-077).

Every route needs a view that carries the access check (``leg_platform.management.access``); a
test walks this module and fails for a route without it. URL names of an area start with its menu
key (``navigation.MENU``), so that the menu marks the entry as active.
"""

from django.urls import path

from leg_platform.management import location_views, views

app_name = "management"

urlpatterns = [
    path("", views.OverviewView.as_view(), name="overview"),
    path("settings/", views.SettingsView.as_view(), name="settings"),
    # The saved column widths of the lists (any list).
    path("lists/<slug:list_key>/widths/", location_views.ListWidthsView.as_view(), name="list_widths"),
    # Locations
    path("locations/", location_views.LocationListView.as_view(), name="locations"),
    path("locations/expanded/", location_views.LocationExpandedView.as_view(), name="locations_expanded"),
    path("locations/new/", location_views.LocationCreateView.as_view(), name="locations_new"),
    path("locations/<int:pk>/", location_views.LocationDetailView.as_view(), name="locations_detail"),
    path("locations/<int:pk>/address/", location_views.LocationAddressView.as_view(), name="locations_address"),
    path("locations/<int:pk>/remark/", location_views.LocationRemarkView.as_view(), name="locations_remark"),
    path("locations/<int:pk>/circuit/", location_views.LocationCircuitView.as_view(), name="locations_circuit"),
    path(
        "locations/<int:pk>/circuit/new/", location_views.LocationCircuitNewView.as_view(),
        name="locations_circuit_new",
    ),
    path(
        "locations/<int:pk>/metering-points/new/", location_views.LocationMeteringPointNewView.as_view(),
        name="locations_metering_point_new",
    ),
    path("locations/<int:pk>/archive/", location_views.LocationArchiveView.as_view(), name="locations_archive"),
    path("locations/<int:pk>/delete/", location_views.LocationDeleteView.as_view(), name="locations_delete"),
    path("locations/<int:pk>/confirm/", location_views.LocationConfirmView.as_view(), name="locations_confirm"),
    path(
        "locations/<int:pk>/suggestion/", location_views.LocationSuggestionView.as_view(),
        name="locations_suggestion",
    ),
    path("transformer-circuits/new/", location_views.CircuitCreateView.as_view(), name="circuits_new"),
    path(
        "transformer-circuits/<int:pk>/edit/", location_views.CircuitEditView.as_view(),
        name="circuits_edit",
    ),
]
