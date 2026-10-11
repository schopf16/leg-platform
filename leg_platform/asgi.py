"""ASGI entry point of LEG-Platform.

https://docs.djangoproject.com/en/6.0/howto/deployment/asgi/
"""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "leg_platform.settings")

application = get_asgi_application()
