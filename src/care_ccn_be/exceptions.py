from typing import Any


class BecknError(Exception):
    """A domain failure that maps to a Beckn NACK body or an ``on_*`` error block."""

    def __init__(self, code: str, message: str, error_type: str = "DOMAIN-ERROR"):
        super().__init__(message)
        self.code = code
        self.message = message
        self.error_type = error_type

    def error_block(self) -> dict[str, Any]:
        return {"type": self.error_type, "code": self.code, "message": self.message}

    def nack_body(self) -> dict[str, Any]:
        return {"message": {"ack": {"status": "NACK"}}, "error": self.error_block()}
