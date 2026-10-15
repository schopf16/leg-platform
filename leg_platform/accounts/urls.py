"""URLs of the portal accounts. The application has no public home page (D-025)."""

from django.urls import path

from leg_platform.accounts import views
from leg_platform.accounts.models import MailJob
from leg_platform.accounts.password_reset import PASSWORD_RESET_LIFETIME
from leg_platform.accounts.registration import REGISTRATION_LINK_LIFETIME

app_name = "accounts"

urlpatterns = [
    path("login/", views.PortalLoginView.as_view(), name="login"),
    path(
        "login/two-factor/",
        views.SecondFactorLoginView.as_view(),
        name="login_second_factor",
    ),
    path("logout/", views.PortalLogoutView.as_view(), name="logout"),
    path("register/", views.RegistrationView.as_view(), name="registration"),
    path(
        "register/sent/",
        views.EmailSentView.as_view(purpose="registration", lifetime=REGISTRATION_LINK_LIFETIME),
        name="registration_sent",
    ),
    path(
        "register/resend/",
        views.ResendView.as_view(
            purpose="registration",
            kind=MailJob.Kind.REGISTRATION,
            start_url="accounts:registration",
            sent_url="accounts:registration_sent",
        ),
        name="registration_resend",
    ),
    path("register/expired/", views.RegistrationInvalidView.as_view(), name="registration_invalid"),
    path("register/password/", views.RegistrationPasswordView.as_view(), name="registration_password"),
    path("register/complete/", views.RegistrationCompleteView.as_view(), name="registration_complete"),
    # Only the token alphabet is matched; the view checks the length.
    path(
        "register/verify/<slug:token>/",
        views.RegistrationVerifyView.as_view(),
        name="registration_verify",
    ),
    path("password-reset/", views.PasswordResetRequestView.as_view(), name="password_reset"),
    path(
        "password-reset/sent/",
        views.EmailSentView.as_view(
            purpose="password_reset", lifetime=PASSWORD_RESET_LIFETIME
        ),
        name="password_reset_sent",
    ),
    path(
        "password-reset/resend/",
        views.ResendView.as_view(
            purpose="password_reset",
            kind=MailJob.Kind.PASSWORD_RESET,
            start_url="accounts:password_reset",
            sent_url="accounts:password_reset_sent",
        ),
        name="password_reset_resend",
    ),
    path(
        "password-reset/expired/",
        views.PasswordResetInvalidView.as_view(),
        name="password_reset_invalid",
    ),
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
    path(
        "email-change/verify/<slug:token>/",
        views.EmailChangeVerifyView.as_view(),
        name="email_change_verify",
    ),
    path(
        "email-change/confirm/",
        views.EmailChangeConfirmView.as_view(),
        name="email_change_confirm",
    ),
    path(
        "email-change/request/",
        views.EmailChangeRequestView.as_view(),
        name="email_change_request",
    ),
    path(
        "email-change/sent/",
        views.EmailChangeSentView.as_view(),
        name="email_change_sent",
    ),
    path(
        "tasks/<slug:key>/hide/",
        views.HideTaskView.as_view(),
        name="task_hide",
    ),
    path(
        "two-factor-reset/verify/<slug:token>/",
        views.TwoFactorResetVerifyView.as_view(),
        name="two_factor_reset_verify",
    ),
    path(
        "two-factor-reset/confirm/",
        views.TwoFactorResetConfirmView.as_view(),
        name="two_factor_reset_confirm",
    ),
    path("profile/", views.ProfileView.as_view(), name="profile"),
    path("profile/edit/", views.ProfileEditView.as_view(), name="profile_edit"),
    path("profile/delete/", views.AccountDeleteView.as_view(), name="account_delete"),
    path("profile/password/", views.PasswordChangeView.as_view(), name="password_change"),
    path("profile/two-factor/", views.TwoFactorOverviewView.as_view(), name="two_factor"),
    path("profile/two-factor/setup/", views.TwoFactorSetupView.as_view(), name="two_factor_setup"),
    path(
        "profile/two-factor/setup/confirm/",
        views.TwoFactorConfirmView.as_view(),
        name="two_factor_confirm",
    ),
    path("profile/two-factor/setup/qr.svg", views.TwoFactorQrView.as_view(), name="two_factor_qr"),
    path(
        "profile/two-factor/recovery-codes/",
        views.RecoveryCodesView.as_view(),
        name="recovery_codes",
    ),
    path(
        "profile/two-factor/disable/",
        views.TwoFactorDisableView.as_view(),
        name="two_factor_disable",
    ),
]
