class IdeGYMException(Exception):
    pass


class InspectionsNotReadyException(IdeGYMException):
    pass


class ResourceDeletionFailedException(IdeGYMException):
    """Raised when one or more Kubernetes resources fail to be deleted."""
