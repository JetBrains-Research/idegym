from abc import ABC, abstractmethod
from dataclasses import dataclass

from idegym.api.image_build import ImageBuildSpec
from idegym.api.status import Status


@dataclass(frozen=True)
class BuildHandle:
    """Opaque, backend-specific identifier for an in-flight or finished build.

    `name` is the string the orchestrator persists as ``JobStatusRecord.job_name`` and
    returns to clients to query status later, so it must be unique and stable for the
    lifetime of the build. Backends may subclass to carry extra fields (project, region,
    namespace, ...) needed by :meth:`ImageBuilder.get_status`; only `name` crosses the
    persistence/API boundary.
    """

    name: str


class ImageBuilder(ABC):
    """Interface every image build backend implements.

    `submit_build` and `get_status` are deliberately split: Kaniko submits a Kubernetes
    Job and then polls it, while Cloud Build submits an asynchronous operation and polls a
    build resource. Both fit the submit-then-poll shape the orchestrator's monitoring loop
    expects.
    """

    #: Default upper bound (seconds) the orchestrator waits for a build to finish before
    #: giving up and recording FAILURE. Backends whose own timeout is configurable should
    #: override :meth:`monitor_timeout` so the two stay consistent.
    DEFAULT_MONITOR_TIMEOUT: float = 2400.0

    def monitor_timeout(self) -> float:
        """How long the orchestrator should poll this builder before declaring failure.

        Must be at least as large as any backend-internal build timeout, otherwise the
        monitor would mark a still-running build as failed.
        """
        return self.DEFAULT_MONITOR_TIMEOUT

    @abstractmethod
    async def submit_build(
        self,
        tag: str,
        spec: ImageBuildSpec,
        *,
        namespace: str,
        service_version: str,
    ) -> BuildHandle:
        """Start building `spec` and pushing it to `tag`; return a handle to track it."""
        ...

    @abstractmethod
    async def get_status(self, handle: BuildHandle) -> Status:
        """Return the current :class:`Status` for a previously submitted build."""
        ...
