"""ASGI entry point of LEG-Platform.

https://docs.djangoproject.com/en/6.0/howto/deployment/asgi/
"""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "leg_platform.settings")

# Nothing to declare here: the address directory is read-only in every process that does not
# declare itself a writer (leg_platform.addresses.storage, D-075).
application = get_asgi_application()
