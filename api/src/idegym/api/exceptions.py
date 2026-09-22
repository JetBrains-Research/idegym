class IdeGYMException(Exception):
    pass


class InspectionsNotReadyException(IdeGYMException):
    pass


class ResourceDeletionFailedException(IdeGYMException):
    """Raised when one or more Kubernetes resources fail to be deleted."""


class KubernetesUnavailableException(IdeGYMException):
    """Raised when a Kubernetes API query fails on every retry, so the resource state is unknown."""
