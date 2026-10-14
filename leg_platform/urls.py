"""URL configuration of LEG-Platform.

https://docs.djangoproject.com/en/6.0/topics/http/urls/
"""

from django.contrib import admin
from django.urls import include, path

from leg_platform.accounts.views import HomeView
from leg_platform.presentation import instance_stylesheet, set_colour_mode

urlpatterns = [
    path("instance.css", instance_stylesheet, name="instance_stylesheet"),
    path("appearance/", set_colour_mode, name="colour_mode"),
    path("addresses/", include("leg_platform.addresses.urls")),
    path("", HomeView.as_view(), name="home"),
    path("", include("leg_platform.accounts.urls")),
    path("admin/", admin.site.urls),
]
