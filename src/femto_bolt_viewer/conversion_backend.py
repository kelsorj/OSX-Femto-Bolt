"""Errors shared by the native converter and acquisition runtime."""


class FrameRejectedError(ValueError):
    """A malformed raw acquisition, not a failed native worker."""
