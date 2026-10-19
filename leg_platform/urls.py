"""URL configuration of LEG-Platform.

https://docs.djangoproject.com/en/6.0/topics/http/urls/
"""

from django.contrib import admin
from django.urls import include, path

from leg_platform.accounts.views import HomeView
from leg_platform.language import set_language
from leg_platform.presentation import instance_favicon, instance_logo, instance_stylesheet

urlpatterns = [
    path("instance.css", instance_stylesheet, name="instance_stylesheet"),
    # The uploaded logos and favicon (D-073): public, because they are on every page, also before the login.
    path("branding/logo/<logo_variant:variant>/", instance_logo, name="instance_logo"),
    path("branding/favicon/", instance_favicon, name="instance_favicon"),
    # The language menu (D-056): POST only, public, no session (second_factor.SESSION_FREE_VIEWS).
    path("language/", set_language, name="set_language"),
    path("addresses/", include("leg_platform.addresses.urls")),
    # The management area for staff users (D-077); every view checks its permissions itself.
    path("management/", include("leg_platform.management.urls")),
    # The customer's participation: buildings, the registration wizard, the contract (D-103).
    path("participation/", include("leg_platform.customer.urls")),
    path("", HomeView.as_view(), name="home"),
    path("", include("leg_platform.accounts.urls")),
    path("admin/", admin.site.urls),
]
