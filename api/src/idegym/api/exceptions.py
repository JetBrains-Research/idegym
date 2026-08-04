class IdeGYMException(Exception):
    pass


class InspectionsNotReadyException(IdeGYMException):
    pass


class ResourceDeletionFailedException(IdeGYMException):
    """Raised when one or more Kubernetes resources fail to be deleted."""


class MigrationError(IdeGYMException):
    """Raised when a database migration cannot be planned or executed.

    Carries an operator-facing message: it surfaces as the failure of a startup, of the
    migration CLI, or of a rollback's downgrade step, and is what tells someone what to
    do next.
    """
