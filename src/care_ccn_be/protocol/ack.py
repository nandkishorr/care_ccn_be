"""ACK / NACK bodies. A NACK is HTTP 200 with a NACK body, as ONIX expects."""

from typing import Any

from pydantic import ValidationError

UNSUPPORTED_ACTION = "70001"
INVALID_PAYLOAD = "70002"


def ack() -> dict[str, Any]:
    return {"message": {"ack": {"status": "ACK"}}}


def nack(code: str, message: str, error_type: str = "DOMAIN-ERROR") -> dict[str, Any]:
    return {
        "message": {"ack": {"status": "NACK"}},
        "error": {"type": error_type, "code": code, "message": message},
    }


def nack_from(exc: ValidationError, code: str = INVALID_PAYLOAD) -> dict[str, Any]:
    reasons = "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in exc.errors()[:5])
    return nack(code, reasons)


def is_nack(body: Any) -> bool:
    if not isinstance(body, dict):
        return False
    message = body.get("message") or {}
    status = message.get("status") or (message.get("ack") or {}).get("status") or ""
    return str(status).upper() == "NACK"
