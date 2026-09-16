from typing import Protocol


class StorageBackend(Protocol):
    """S3-shaped storage interface. Swap the implementation, not the callers."""

    def save(self, key: str, content: bytes) -> str:
        """Persist `content` under `key`, return a URI to store in the DB."""
        ...

    def read(self, uri: str) -> bytes:
        """Fetch the bytes previously stored at a URI returned by `save`."""
        ...
