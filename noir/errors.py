class NoirError(Exception):
    """Base error. Never include credentials or SSHX URLs in error messages."""


class UserError(NoirError):
    """An actionable, safe-to-display error."""


class AccessDenied(UserError):
    pass


class NotFound(UserError):
    pass


class BackendError(NoirError):
    """Infrastructure failure; details belong in operator logs, not Discord."""
