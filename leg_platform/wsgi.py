"""WSGI entry point of LEG-Platform.

https://docs.djangoproject.com/en/6.0/howto/deployment/wsgi/
"""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "leg_platform.settings")

# Nothing to declare here: the address directory is read-only in every process that does not
# declare itself a writer (leg_platform.addresses.storage, D-075).
application = get_wsgi_application()
