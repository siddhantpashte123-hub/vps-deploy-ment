class FierError(Exception):
    """Base error. Never include credentials or SSHX URLs in error messages."""


class UserError(FierError):
    """An actionable, safe-to-display error."""


class AccessDenied(UserError):
    pass


class NotFound(UserError):
    pass


class BackendError(FierError):
    """Infrastructure failure; details belong in operator logs, not Discord."""
