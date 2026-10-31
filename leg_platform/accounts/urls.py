"""URLs of the portal accounts. The application has no public home page (D-025)."""

from django.urls import path

from leg_platform import render_as
from leg_platform.accounts import method_views, passkey_views, profile_views, registration_views, views
from leg_platform.accounts.models import MailJob
from leg_platform.accounts.password_reset import PASSWORD_RESET_LIFETIME
from leg_platform.accounts.registration import REGISTRATION_LINK_LIFETIME

app_name = "accounts"

urlpatterns = [
    path("sign-in-method/password/", method_views.PasswordMethodView.as_view(), name="method_password"),
    path("sign-in-method/app/", method_views.AppMethodView.as_view(), name="method_app"),
    path("sign-in-method/app/qr.svg", method_views.AppMethodQrView.as_view(), name="method_app_qr"),
    path("sign-in-method/verify/<slug:token>/", method_views.ReturnVerifyView.as_view(), name="method_return_verify"),
    path("sign-in-method/confirm/", method_views.ReturnConfirmView.as_view(), name="method_return_confirm"),
    path("recovery/", method_views.RecoveryRequestView.as_view(), name="recovery"),
    path("recovery/sent/", views.LinkNoLongerValidView.as_view(
        template_name="accounts/recovery_sent.html"
    ), name="recovery_sent"),
    path("passkeys/options/", passkey_views.OptionsView.as_view(), name="passkey_options"),
    path("passkeys/login/", passkey_views.LoginView.as_view(), name="passkey_login"),
    path("passkeys/setup/", passkey_views.SetupView.as_view(), name="passkey_setup"),
    path("passkeys/", passkey_views.ManageView.as_view(), name="passkeys"),
    path("login/", views.PortalLoginView.as_view(), name="login"),
    path("login/help/", method_views.LoginHelpView.as_view(), name="login_help"),
    path(
        "login/two-factor/",
        views.SecondFactorLoginView.as_view(),
        name="login_second_factor",
    ),
    path("logout/", views.PortalLogoutView.as_view(), name="logout"),
    # End of the support view "View as" (D-131): POST only.
    path("view-as/end/", render_as.EndView.as_view(), name="render_as_end"),
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
    path("register/method/", registration_views.RegistrationMethodView.as_view(), name="registration_method"),
    path("register/password/", views.RegistrationPasswordView.as_view(), name="registration_password"),
    path("register/app/", registration_views.RegistrationAppView.as_view(), name="registration_app"),
    path("register/app/qr.svg", registration_views.RegistrationAppQrView.as_view(), name="registration_app_qr"),
    path("register/passkey/", registration_views.RegistrationPasskeyView.as_view(), name="registration_passkey"),
    path(
        "register/passkey/options/",
        registration_views.RegistrationPasskeyOptionsView.as_view(),
        name="registration_passkey_options",
    ),
    path(
        "register/passkey/create/",
        registration_views.RegistrationPasskeyCreateView.as_view(),
        name="registration_passkey_create",
    ),
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
    # "Link no longer valid" pages (D-067): their paths must not match a token route (no "verify/" or "stop/" prefix).
    path(
        "email-change/expired/",
        views.LinkNoLongerValidView.as_view(template_name="accounts/email_change_invalid.html"),
        name="email_change_invalid",
    ),
    path(
        "email-change/stop-expired/",
        views.LinkNoLongerValidView.as_view(template_name="accounts/email_change_stop_invalid.html"),
        name="email_change_stop_invalid",
    ),
    path(
        "two-factor-reset/expired/",
        views.LinkNoLongerValidView.as_view(template_name="accounts/two_factor_reset_invalid.html"),
        name="two_factor_reset_invalid",
    ),
    path(
        "email-change/confirm/",
        views.EmailChangeConfirmView.as_view(),
        name="email_change_confirm",
    ),
    path(
        "email-change/stop/<slug:token>/",
        views.EmailChangeStopVerifyView.as_view(),
        name="email_change_stop_verify",
    ),
    path(
        "email-change/stop/",
        views.EmailChangeStopConfirmView.as_view(),
        name="email_change_stop_confirm",
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
    path("profile/", profile_views.ProfileView.as_view(), name="profile"),
    path("profile/edit/", profile_views.ProfileEditView.as_view(), name="profile_edit"),
    path(
        "profile/email/",
        profile_views.EmailChangeRequestView.as_view(),
        name="email_change_request",
    ),
    path(
        "profile/email/cancel/<slug:kind>/",
        profile_views.EmailChangeCancelView.as_view(),
        name="email_change_cancel",
    ),
    path("profile/delete/", profile_views.AccountDeleteView.as_view(), name="account_delete"),
    path("profile/end/", profile_views.AccountEndView.as_view(), name="account_end"),
    path("profile/password/", profile_views.PasswordChangeView.as_view(), name="password_change"),
    path("profile/two-factor/", profile_views.TwoFactorOverviewView.as_view(), name="two_factor"),
    path("profile/sign-in-method/", profile_views.LoginMethodView.as_view(), name="login_method"),
    path(
        "profile/two-factor/setup/",
        profile_views.TwoFactorSetupView.as_view(),
        name="two_factor_setup",
    ),
    path(
        "profile/two-factor/setup/confirm/",
        profile_views.TwoFactorConfirmView.as_view(),
        name="two_factor_confirm",
    ),
    path(
        "profile/two-factor/setup/qr.svg",
        profile_views.TwoFactorQrView.as_view(),
        name="two_factor_qr",
    ),
    path(
        "profile/two-factor/recovery-codes/",
        profile_views.RecoveryCodesView.as_view(),
        name="recovery_codes",
    ),
    path(
        "profile/two-factor/recovery-codes/done/",
        profile_views.RecoveryCodesDoneView.as_view(),
        name="recovery_codes_done",
    ),
    path(
        "profile/two-factor/disable/",
        profile_views.TwoFactorDisableView.as_view(),
        name="two_factor_disable",
    ),
    path(
        "profile/two-factor/lost-phone/",
        profile_views.TwoFactorLostPhoneView.as_view(),
        name="two_factor_lost_phone",
    ),
]
