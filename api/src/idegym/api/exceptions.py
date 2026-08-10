class IdeGYMException(Exception):
    pass


class InspectionsNotReadyException(IdeGYMException):
    pass


class ResourceDeletionFailedException(IdeGYMException):
    """Raised when one or more Kubernetes resources fail to be deleted."""


class MigrationError(IdeGYMException):
    """Raised when a database migration cannot be planned or executed.

    The message is operator-facing: it surfaces as a failed startup, CLI run, or rollback
    downgrade, and is what says what to do next.
    """
