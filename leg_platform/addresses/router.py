"""Keeps the address directory in its own SQLite file and the import queue in the application database.

``Directory`` and ``Address`` belong to the alias ``addresses`` (the SQLite file of the directory,
not part of any backup); ``ImportRequest`` belongs to ``default`` (D-075). Reads and writes of a
model go to the alias of the model, so no code needs ``using()``. ``allow_migrate`` makes
``migrate`` create the directory tables only in the file and every other table only in the
application database, and ``migrate --database=addresses`` create nothing else in the file.
"""

from .storage import DIRECTORY_ALIAS

APPLICATION_ALIAS = "default"
DIRECTORY_MODELS = {"address", "directory"}


def _is_directory_model(model):
    return model._meta.app_label == "addresses" and model._meta.model_name in DIRECTORY_MODELS


class AddressRouter:
    def db_for_read(self, model, **hints):
        return DIRECTORY_ALIAS if _is_directory_model(model) else None

    def db_for_write(self, model, **hints):
        return DIRECTORY_ALIAS if _is_directory_model(model) else None

    def allow_relation(self, obj1, obj2, **hints):
        # No relation connects the directory and anything else.
        if _is_directory_model(obj1) or _is_directory_model(obj2):
            return _is_directory_model(obj1) and _is_directory_model(obj2)
        return None

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        # Django passes the model name in lower case to the operations and as the object name
        # (capitalised) to makemigrations' consistency check.
        name = model_name.lower() if model_name else None
        if db == DIRECTORY_ALIAS:
            # Only the two directory tables (and Django's own record of applied migrations,
            # which does not ask the router) are created in the file.
            return app_label == "addresses" and name in DIRECTORY_MODELS
        if app_label == "addresses" and name in DIRECTORY_MODELS:
            return False
        return None
