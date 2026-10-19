from __future__ import annotations


class MediaError(Exception):
    """A failure safe to show the creator: stable code, plain message, no paths or keys.

    ``detail`` is for operators (traces, job records) and must never reach the UI.
    """

    def __init__(self, code: str, message: str, *, retryable: bool = False, detail: str = ""):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.retryable = retryable
        self.detail = detail

    def to_public(self, asset_id: str | None = None) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "asset_id": asset_id,
            "retryable": self.retryable,
        }
