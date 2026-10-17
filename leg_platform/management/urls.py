"""URLs of the management area, under ``/management/`` (D-077).

Every route needs a view that carries the access check (``leg_platform.management.access``); a
test walks this module and fails for a route without it. URL names of an area start with its menu
key (``navigation.MENU``), so that the menu marks the entry as active.
"""

from django.urls import path

from leg_platform.management import leg_views, location_views, person_views, views
from leg_platform.management import metering_point_views as mp_views
from leg_platform.management.printing import views as print_views

app_name = "management"

urlpatterns = [
    path("", views.OverviewView.as_view(), name="overview"),
    path("settings/", views.SettingsView.as_view(), name="settings"),
    path("settings/check/", views.SettingsFieldCheckView.as_view(), name="settings_check"),
    # The saved column widths of the lists (any list).
    path("lists/<slug:list_key>/widths/", location_views.ListWidthsView.as_view(), name="list_widths"),
    # Persons (an account with a customer number; the id is the id of the account)
    path("persons/", person_views.PersonListView.as_view(), name="persons"),
    path("persons/print/", print_views.PersonPrintView.as_view(), name="persons_print"),
    path("persons/new/", person_views.PersonCreateView.as_view(), name="persons_new"),
    path("persons/<int:pk>/", person_views.PersonDetailView.as_view(), name="persons_detail"),
    path("persons/<int:pk>/note/", person_views.PersonNoteView.as_view(), name="persons_note"),
    path("persons/<int:pk>/details/", person_views.PersonDetailsView.as_view(), name="persons_details"),
    path("persons/<int:pk>/contact/", person_views.PersonContactView.as_view(), name="persons_contact"),
    path("persons/<int:pk>/billing-address/", person_views.PersonAddressView.as_view(), name="persons_address"),
    path("persons/<int:pk>/payment/", person_views.PersonPaymentView.as_view(), name="persons_payment"),
    path(
        "persons/<int:pk>/invitation/", person_views.PersonInvitationView.as_view(), name="persons_invitation"
    ),
    path("persons/<int:pk>/shares/", person_views.PersonSharesView.as_view(), name="persons_shares"),
    path(
        "persons/<int:pk>/address-suggestion/", person_views.PersonAddressSuggestionView.as_view(),
        name="persons_address_suggestion",
    ),
    # Locations
    path("locations/", location_views.LocationListView.as_view(), name="locations"),
    path("locations/print/", print_views.LocationPrintView.as_view(), name="locations_print"),
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
    # Metering points
    path("metering-points/", mp_views.MeteringPointListView.as_view(), name="metering_points"),
    path("metering-points/print/", print_views.MeteringPointPrintView.as_view(), name="metering_points_print"),
    path("metering-points/new/", mp_views.MeteringPointNewView.as_view(), name="metering_points_new"),
    path(
        "metering-points/check-number/", mp_views.CheckNumberView.as_view(), name="metering_points_check_number"
    ),
    path(
        "metering-points/location-search/", mp_views.LocationSearchView.as_view(),
        name="metering_points_location_search",
    ),
    path(
        "metering-points/person-search/", mp_views.PersonSearchView.as_view(), name="metering_points_person_search"
    ),
    path("metering-points/<int:pk>/", mp_views.MeteringPointDetailView.as_view(), name="metering_points_detail"),
    path("metering-points/<int:pk>/notify/", mp_views.NotifyView.as_view(), name="metering_points_notify"),
    path("metering-points/<int:pk>/confirm/", mp_views.ConfirmView.as_view(), name="metering_points_confirm"),
    path("metering-points/<int:pk>/start-date/", mp_views.StartDateView.as_view(), name="metering_points_start"),
    path("metering-points/<int:pk>/terminate/", mp_views.TerminateView.as_view(), name="metering_points_terminate"),
    path("metering-points/<int:pk>/leg/", mp_views.LegView.as_view(), name="metering_points_leg"),
    path("metering-points/<int:pk>/person/", mp_views.PersonView.as_view(), name="metering_points_person"),
    path(
        "metering-points/<int:pk>/person/end/", mp_views.PersonEndView.as_view(), name="metering_points_person_end"
    ),
    path(
        "metering-points/<int:pk>/person/delete/", mp_views.PersonDeleteView.as_view(),
        name="metering_points_person_delete",
    ),
    path("metering-points/<int:pk>/archive/", mp_views.ArchiveView.as_view(), name="metering_points_archive"),
    path("metering-points/<int:pk>/delete/", mp_views.DeleteView.as_view(), name="metering_points_delete"),
    # LEGs
    path("legs/", leg_views.LegListView.as_view(), name="legs"),
    path("legs/print/", print_views.LegPrintView.as_view(), name="legs_print"),
    path("legs/new/", leg_views.LegCreateView.as_view(), name="legs_new"),
    path("legs/<int:pk>/", leg_views.LegDetailView.as_view(), name="legs_detail"),
    path("legs/<int:pk>/quota/", leg_views.LegQuotaView.as_view(), name="legs_quota"),
    path("legs/<int:pk>/edit/", leg_views.LegMasterDataView.as_view(), name="legs_edit"),
    path("legs/<int:pk>/archive/", leg_views.LegArchiveView.as_view(), name="legs_archive"),
    path("legs/<int:pk>/delete/", leg_views.LegDeleteView.as_view(), name="legs_delete"),
]
