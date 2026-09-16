class LockstepError(Exception):
    """Base class for domain errors that map to HTTP responses."""


class NotFoundError(LockstepError):
    pass


class ValidationError(LockstepError):
    pass


class AuthError(LockstepError):
    pass


class IngestionError(LockstepError):
    """Raised when a required column can't be resolved in an uploaded CSV."""
