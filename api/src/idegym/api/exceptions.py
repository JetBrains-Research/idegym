from typing import List, Optional


class IdeGYMException(Exception):
    pass


class InspectionsNotReadyException(IdeGYMException):
    pass


class ResourceDeletionFailedException(IdeGYMException):
    """Raised when one or more Kubernetes resources fail to be deleted.

    Attributes:
        failures: A list of human-readable resource identifiers that failed to delete.
    """

    def __init__(self, failures: Optional[List[str]] = None):
        self.failures = failures
        message = f"Failed to delete {', '.join(failures)}" if failures else "Failed to delete resources"
        super().__init__(message)
