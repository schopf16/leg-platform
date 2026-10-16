"""URL configuration of LEG-Platform.

https://docs.djangoproject.com/en/6.0/topics/http/urls/
"""

from django.contrib import admin
from django.urls import include, path

from leg_platform.accounts.views import HomeView
from leg_platform.presentation import instance_logo, instance_stylesheet

urlpatterns = [
    path("instance.css", instance_stylesheet, name="instance_stylesheet"),
    # The uploaded logos (D-073): public, because they are on every page, also before the login.
    path("branding/logo/<logo_variant:variant>/", instance_logo, name="instance_logo"),
    path("addresses/", include("leg_platform.addresses.urls")),
    # The management area for staff users (D-077); every view checks its permissions itself.
    path("management/", include("leg_platform.management.urls")),
    path("", HomeView.as_view(), name="home"),
    path("", include("leg_platform.accounts.urls")),
    path("admin/", admin.site.urls),
]
