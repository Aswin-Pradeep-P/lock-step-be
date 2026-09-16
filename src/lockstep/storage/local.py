from pathlib import Path


class LocalDiskStorage:
    """Local-disk implementation of StorageBackend, behind an S3-shaped interface.

    Swapping to real S3 later means writing an S3Storage class with the same
    `save`/`read` signatures — no caller changes.
    """

    def __init__(self, root_dir: str):
        self.root = Path(root_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, key: str, content: bytes) -> str:
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return f"file://{path}"

    def read(self, uri: str) -> bytes:
        if not uri.startswith("file://"):
            raise ValueError(f"LocalDiskStorage cannot read URI: {uri}")
        path = Path(uri.removeprefix("file://")).resolve()
        if self.root not in path.parents and path != self.root:
            raise ValueError("Refusing to read path outside the upload root")
        return path.read_bytes()

    def _resolve(self, key: str) -> Path:
        # `key` is always composed server-side (run id + a generated filename),
        # never taken verbatim from user input.
        path = (self.root / key).resolve()
        if self.root not in path.parents and path != self.root:
            raise ValueError("Refusing to write outside the upload root")
        return path
