from django.urls import path

from .views import address_lookup

app_name = "addresses"
urlpatterns = [path("lookup/", address_lookup, name="lookup")]
