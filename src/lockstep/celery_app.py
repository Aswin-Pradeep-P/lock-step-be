from celery import Celery

from lockstep.config import get_settings

settings = get_settings()

celery_app = Celery(
    "lockstep",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["lockstep.tasks.reconciliation_tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_track_started=True,
    broker_connection_retry_on_startup=True,
)
