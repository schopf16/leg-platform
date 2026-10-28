"""Routes of the customer's participation under ``/participation/``, namespace ``customer`` (D-103).

Every callback carries the access check (``customer.access.CustomerAccessMixin``); the walk in
``customer/tests/test_access.py`` fails for a route without it. Object ids are resolved through the logged-in account in
the views and services.
"""

from django.urls import path

from leg_platform.customer import application_edit, deregistration, views, wizard

app_name = "customer"

urlpatterns = [
    path("buildings/", views.BuildingsView.as_view(), name="buildings"),
    path("neighbourhood/", views.NeighbourhoodView.as_view(), name="neighbourhood"),
    path("buildings/<int:pk>/details/", views.BuildingDetailsView.as_view(), name="building_details"),
    path("metering-points/<int:pk>/name/", views.MeteringPointNameView.as_view(), name="metering_point_name"),
    path("application/", wizard.ApplicationStartView.as_view(), name="application_start"),
    path("application/<int:pk>/", wizard.ApplicationResumeView.as_view(), name="application"),
    path("application/<int:pk>/building/", wizard.BuildingStepView.as_view(), name="application_building"),
    path(
        "application/<int:pk>/metering-points/", wizard.MeteringPointsStepView.as_view(),
        name="application_metering_points",
    ),
    path(
        "application/<int:pk>/bank-connection/", wizard.BankStepView.as_view(), name="application_bank_connection"
    ),
    path("application/<int:pk>/contract/", wizard.ContractStepView.as_view(), name="application_contract"),
    path("application/<int:pk>/start-date/", wizard.StartDateStepView.as_view(), name="application_start_date"),
    path("application/<int:pk>/cancel/", wizard.ApplicationCancelView.as_view(), name="application_cancel"),
    path("application/<int:pk>/submitted/", wizard.ApplicationSubmittedView.as_view(), name="application_submitted"),
    path("application/<int:pk>/withdraw/", wizard.ApplicationWithdrawView.as_view(), name="application_withdraw"),
    path("application/<int:pk>/change/", wizard.ApplicationChangeView.as_view(), name="application_change"),
    # Single details of a submitted application, edited in place (only while state is submitted).
    path("application/<int:pk>/edit/start-date/", application_edit.StartEditView.as_view(), name="application_edit_start"),
    path(
        "application/<int:pk>/edit/bank-connection/", application_edit.BankEditView.as_view(),
        name="application_edit_bank",
    ),
    path(
        "application/<int:pk>/edit/metering-point/<int:point_id>/", application_edit.PointEditView.as_view(),
        name="application_edit_point",
    ),
    path(
        "application/<int:pk>/edit/building/<int:building_id>/", application_edit.AddressEditView.as_view(),
        name="application_edit_address",
    ),
    path("contract/", views.ContractView.as_view(), name="contract"),
    path("contact/", views.ContactView.as_view(), name="contact"),
    path("contract/accept/", views.ContractAcceptView.as_view(), name="contract_accept"),
    path("contract/download/", views.ContractDownloadView.as_view(), name="contract_download"),
    path("contract/read/", views.ContractReadingView.as_view(), name="contract_read"),
    path(
        "contract/<int:version_id>/download/", views.ContractDownloadView.as_view(), name="contract_version_download"
    ),
    path("deregistration/", deregistration.DeregistrationStartView.as_view(), name="deregistration_start"),
    path("deregistration/<int:pk>/", deregistration.DeregistrationDoneView.as_view(), name="deregistration_done"),
    path("bank-connection/", views.BankConnectionView.as_view(), name="bank_connection"),
]
