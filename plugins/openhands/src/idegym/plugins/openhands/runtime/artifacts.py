"""Artifact store for oversized outputs.

Large terminal logs, screenshots, and recordings are written under a service-owned output
directory. Results carry an opaque artifact id and a retrieval URL — never a raw host path.
"""

import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from idegym.plugins.openhands.api.errors import ErrorCode, ServiceError
from idegym.plugins.openhands.api.models import ArtifactDescriptor
from idegym.plugins.openhands.api.names import PUBLIC_PREFIX


class _Entry:
    __slots__ = ("descriptor", "path")

    def __init__(self, descriptor: ArtifactDescriptor, path: Path) -> None:
        self.descriptor = descriptor
        self.path = path


class ArtifactStore:
    def __init__(
        self,
        output_dir: str,
        *,
        max_artifacts: int = 256,
        max_total_bytes: int = 512_000_000,
        max_single_bytes: int = 33_554_432,
    ) -> None:
        self._dir = Path(output_dir)
        self._max_artifacts = max_artifacts
        self._max_total_bytes = max_total_bytes
        self._max_single_bytes = max_single_bytes
        self._entries: "OrderedDict[str, _Entry]" = OrderedDict()
        self._total_bytes = 0

    def _public_url(self, artifact_id: str) -> str:
        return f"/api{PUBLIC_PREFIX}/artifacts/{artifact_id}"

    def save(
        self, content: bytes, *, media_type: str = "text/plain", filename: Optional[str] = None
    ) -> ArtifactDescriptor:
        # Hard per-artifact cap: bound memory/disk so a single result cannot store hundreds of MB.
        if len(content) > self._max_single_bytes:
            content = content[: self._max_single_bytes]
        self._dir.mkdir(parents=True, exist_ok=True)
        artifact_id = uuid.uuid4().hex
        path = self._dir / artifact_id
        path.write_bytes(content)
        descriptor = ArtifactDescriptor(
            artifact_id=artifact_id,
            media_type=media_type,
            size_bytes=len(content),
            filename=filename,
            url=self._public_url(artifact_id),
            created_at=datetime.now(timezone.utc),
        )
        self._entries[artifact_id] = _Entry(descriptor, path)
        self._total_bytes += len(content)
        self._evict()
        return descriptor

    def save_text(self, text: str, *, filename: Optional[str] = None) -> ArtifactDescriptor:
        return self.save(text.encode("utf-8"), media_type="text/plain; charset=utf-8", filename=filename)

    def _evict(self) -> None:
        # Bounded retention: drop oldest artifacts by count and total bytes. Always keep at least the
        # most recently saved artifact, so save() never evicts the entry it just returned (even when
        # that single artifact exceeds max_total_bytes).
        while len(self._entries) > 1 and (
            len(self._entries) > self._max_artifacts or self._total_bytes > self._max_total_bytes
        ):
            _, entry = self._entries.popitem(last=False)
            self._total_bytes -= entry.descriptor.size_bytes
            entry.path.unlink(missing_ok=True)

    def get_metadata(self, artifact_id: str) -> ArtifactDescriptor:
        entry = self._entries.get(artifact_id)
        if entry is None:
            raise ServiceError(ErrorCode.UNKNOWN_ARTIFACT, f"Unknown artifact: {artifact_id}")
        return entry.descriptor

    def read(self, artifact_id: str) -> tuple[bytes, ArtifactDescriptor]:
        entry = self._entries.get(artifact_id)
        if entry is None or not entry.path.exists():
            raise ServiceError(ErrorCode.UNKNOWN_ARTIFACT, f"Unknown artifact: {artifact_id}")
        return entry.path.read_bytes(), entry.descriptor

    def get_path(self, artifact_id: str) -> tuple[Path, ArtifactDescriptor]:
        """Return the on-disk path + descriptor so callers can stream the file instead of buffering."""
        entry = self._entries.get(artifact_id)
        if entry is None or not entry.path.exists():
            raise ServiceError(ErrorCode.UNKNOWN_ARTIFACT, f"Unknown artifact: {artifact_id}")
        return entry.path, entry.descriptor

    def clear(self) -> int:
        count = len(self._entries)
        for entry in self._entries.values():
            entry.path.unlink(missing_ok=True)
        self._entries.clear()
        self._total_bytes = 0
        return count

    def purge_storage(self) -> int:
        """Remove every file in the service-owned output dir and reset the in-memory index.

        Artifact metadata lives only in memory, so a file left by a previous process is unreachable
        through the API and untracked by quota accounting/eviction. Called at startup so repeated
        restarts do not accumulate permanently orphaned disk usage.
        """
        self._entries.clear()
        self._total_bytes = 0
        removed = 0
        if self._dir.exists():
            for child in self._dir.iterdir():
                if child.is_file():
                    child.unlink(missing_ok=True)
                    removed += 1
        return removed
