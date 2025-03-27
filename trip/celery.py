# project/celery.py
import os
from celery import Celery

# Set the default Django settings module
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "trip.settings")

celery_app = Celery("project")

# Load settings from Django settings.py
celery_app.config_from_object("django.conf:settings", namespace="CELERY")

# Auto-discover tasks in Django apps
celery_app.autodiscover_tasks()

@celery_app.task(bind=True)
def debug_task(self):
    print(f"Request: {self.request!r}")