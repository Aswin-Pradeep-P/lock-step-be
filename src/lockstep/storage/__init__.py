from functools import lru_cache

from lockstep.config import get_settings
from lockstep.storage.base import StorageBackend
from lockstep.storage.local import LocalDiskStorage


@lru_cache
def get_storage() -> StorageBackend:
    settings = get_settings()
    if settings.storage_backend == "local":
        return LocalDiskStorage(settings.upload_dir)
    raise ValueError(f"Unknown storage backend: {settings.storage_backend}")


__all__ = ["StorageBackend", "get_storage"]
