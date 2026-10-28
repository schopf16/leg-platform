"""Routes ``Directory``/``Address`` to the ``addresses`` SQLite file and everything else (incl. ``ImportRequest``) to ``default`` (D-075)."""

from .storage import DIRECTORY_ALIAS

DIRECTORY_MODELS = {
    "address", "addressplace", "addressstreet", "directory", "plant", "plantregister", "transformerstation",
}  # plant: D-117, stations: D-118


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
        # Django passes the model name lower case to operations, capitalised to makemigrations' check.
        name = model_name.lower() if model_name else None
        if db == DIRECTORY_ALIAS:
            # Only the two directory tables (Django's migration record does not ask the router).
            return app_label == "addresses" and name in DIRECTORY_MODELS
        if app_label == "addresses" and name in DIRECTORY_MODELS:
            return False
        return None
