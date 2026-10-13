"""URLs of the portal accounts. The application has no public home page (D-025)."""

from django.urls import path

from leg_platform.accounts import views

app_name = "accounts"

urlpatterns = [
    path("login/", views.PortalLoginView.as_view(), name="login"),
    path("logout/", views.PortalLogoutView.as_view(), name="logout"),
    path("register/", views.RegistrationView.as_view(), name="registration"),
    path("register/sent/", views.EmailSentView.as_view(), name="registration_sent"),
    path("register/complete/", views.RegistrationCompleteView.as_view(), name="registration_complete"),
    # Only the token alphabet is matched; the view checks the length.
    path(
        "register/verify/<slug:token>/",
        views.RegistrationVerifyView.as_view(),
        name="registration_verify",
    ),
    path("password-reset/", views.PasswordResetRequestView.as_view(), name="password_reset"),
    path("password-reset/sent/", views.EmailSentView.as_view(), name="password_reset_sent"),
    path(
        "password-reset/new-password/",
        views.PasswordResetSetView.as_view(),
        name="password_reset_set",
    ),
    path(
        "password-reset/verify/<slug:token>/",
        views.PasswordResetVerifyView.as_view(),
        name="password_reset_verify",
    ),
    path("profile/", views.ProfileView.as_view(), name="profile"),
]
