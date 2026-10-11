"""Errors shared by the cloud clients."""


class NinjaCloudError(Exception):
    """Base exception."""


class AuthError(NinjaCloudError):
    """Bad credentials or expired session."""


class TransportError(NinjaCloudError):
    """Network / 5xx / unexpected payload."""
