"""HTTP delivery to the ONIX caller. Pure transport: returns an ``AckResult``, never touches the DB."""

import json
import logging
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any

import requests

from care_ccn_be.protocol.ack import is_nack

logger = logging.getLogger(__name__)

ACK = "ack"
NACK = "nack"
ERROR = "error"
SKIPPED = "skipped"


class TransportError(Exception):
    """The POST itself failed (network, timeout)."""


@dataclass
class AckResult:
    result: str
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.result == ACK

    @property
    def retryable(self) -> bool:
        return self.result == ERROR or (
            self.result == NACK and self.detail.get("statusCode", 0) >= HTTPStatus.INTERNAL_SERVER_ERROR
        )


class BecknClient:
    def __init__(self, base_url: str, timeout: int = 10):
        self.base_url = (base_url or "").rstrip("/")
        self.timeout = timeout

    def post(self, action: str, payload: dict[str, Any]) -> AckResult:
        if not self.base_url:
            return AckResult(SKIPPED, {"reason": "caller URL not configured"})
        url = f"{self.base_url}/{action}"
        context = payload.get("context") or {}
        logger.info(
            "Beckn -> %s (transactionId=%s) payload=%s",
            url,
            context.get("transactionId"),
            json.dumps(payload, default=str),
        )
        try:
            response = requests.post(url, json=payload, timeout=self.timeout)
        except requests.RequestException as exc:
            logger.warning("Beckn delivery to %s failed: %s", url, exc)
            return AckResult(ERROR, {"reason": f"delivery failed: {exc}"})

        try:
            body: Any = response.json()
        except ValueError:
            body = response.text
        detail: dict[str, Any] = {"statusCode": response.status_code, "body": body}
        if isinstance(body, dict) and body.get("error"):
            detail["error"] = body["error"]
        if response.status_code != HTTPStatus.OK or is_nack(body):
            return AckResult(NACK, detail)
        return AckResult(ACK, detail)
