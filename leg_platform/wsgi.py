"""WSGI entry point of LEG-Platform.

https://docs.djangoproject.com/en/6.0/howto/deployment/wsgi/
"""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "leg_platform.settings")

application = get_wsgi_application()
