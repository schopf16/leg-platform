"""Routing is defence in depth; SELECT-only database grants are mandatory.

Explicit QuerySet.using() bypasses routing. Never configure address_import in a
web process. Configure it only for the trusted importer/migration process.
"""

from django.core.exceptions import ImproperlyConfigured

from .models import APPLICATION_ALIAS, READ_ALIAS, WRITE_ALIAS

DIRECTORY_MODELS = {"address", "directory"}


class AddressRouter:
    def db_for_read(self, model, **hints):
        if model._meta.app_label == "addresses":
            return READ_ALIAS if model._meta.model_name in DIRECTORY_MODELS else APPLICATION_ALIAS
        return None

    def db_for_write(self, model, **hints):
        if model._meta.app_label == "addresses":
            if model._meta.model_name in DIRECTORY_MODELS:
                raise ImproperlyConfigured("Directory writes require the explicit importer connection.")
            return APPLICATION_ALIAS
        return None

    def allow_relation(self, obj1, obj2, **hints):
        if "addresses" in {obj1._meta.app_label, obj2._meta.app_label}:
            return False
        return None

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        if app_label == "addresses":
            if model_name in DIRECTORY_MODELS:
                return db == WRITE_ALIAS
            if model_name == "importrequest":
                return db == APPLICATION_ALIAS
            return False
        if db in {READ_ALIAS, WRITE_ALIAS}:
            return False
        return None
