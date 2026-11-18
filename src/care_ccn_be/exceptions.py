from typing import Any


class BecknError(Exception):
    """A domain failure that maps to a Beckn NACK body or an ``on_*`` error block."""

    def __init__(self, code: str, message: str, error_type: str = "DOMAIN-ERROR", paths: list[str] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.error_type = error_type
        self.paths = paths or []

    def error_block(self) -> dict[str, Any]:
        block = {"type": self.error_type, "code": self.code, "message": self.message}
        if self.paths:
            block["paths"] = self.paths
        return block

    def nack_body(self) -> dict[str, Any]:
        return {"message": {"ack": {"status": "NACK"}}, "error": self.error_block()}
