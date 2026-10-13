"""URL configuration of LEG-Platform.

https://docs.djangoproject.com/en/6.0/topics/http/urls/
"""

from django.contrib import admin
from django.urls import include, path

from leg_platform.accounts.views import HomeView

urlpatterns = [
    path("", HomeView.as_view(), name="home"),
    path("", include("leg_platform.accounts.urls")),
    path("admin/", admin.site.urls),
]
