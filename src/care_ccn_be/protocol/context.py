"""Context construction for outbound requests, callbacks, unsolicited callbacks and catalog publishes."""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, ClassVar

from care_ccn_be.protocol.actions import DISCOVER, callback_of

# Keys a caller (the frontend) may override on an outbound request context.
OVERRIDABLE = frozenset({"networkId", "version", "bppId", "bppUri", "ttl", "schemaContext"})
BPP_KEYS = ("bppId", "bppUri")


@dataclass(frozen=True)
class Endpoint:
    id: str = ""
    uri: str = ""


@dataclass(frozen=True)
class NetworkIdentity:
    """Who this Care instance is on the network. Built from plugin settings by the flow layer."""

    network_id: str = ""
    version: str = "2.0.0"
    bap: Endpoint = field(default_factory=Endpoint)
    bpp: Endpoint = field(default_factory=Endpoint)


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def new_id() -> str:
    return str(uuid.uuid4())


def clean_overrides(action: str, overrides: Mapping[str, Any] | None) -> dict[str, Any]:
    """Only whitelisted, non-null overrides; a discover is a broadcast and never carries a BPP."""
    cleaned = {k: v for k, v in (overrides or {}).items() if k in OVERRIDABLE and v is not None}
    if action == DISCOVER:
        for key in BPP_KEYS:
            cleaned.pop(key, None)
    return cleaned


def _compact(context: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in context.items() if v not in (None, "")}


class ContextBuilder:
    """Base builder. Subclasses (per flow) supply the default BPP and the discover schema context."""

    discover_schema_context: ClassVar[str | None] = None

    def __init__(self, identity: NetworkIdentity, reuse_message_id: bool = True):
        self.identity = identity
        self.reuse_message_id = reuse_message_id

    def default_bpp(self) -> Endpoint:
        return Endpoint()

    def request(
        self,
        action: str,
        transaction_id: str,
        routing: Mapping[str, Any] | None = None,
        overrides: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Outbound BAP request. ``routing`` (learned from ``on_discover``) beats the flow default."""
        routing = routing or {}
        if action == DISCOVER:
            bpp_id, bpp_uri = routing.get("bppId"), routing.get("bppUri")
        else:
            default = self.default_bpp()
            bpp_id = routing.get("bppId") or default.id or None
            bpp_uri = routing.get("bppUri") or default.uri or None
        context: dict[str, Any] = {
            "networkId": self.identity.network_id,
            "action": action,
            "version": self.identity.version,
            "bapId": self.identity.bap.id,
            "bapUri": self.identity.bap.uri,
            "transactionId": transaction_id,
            "messageId": new_id(),
            "timestamp": now_iso(),
            "bppId": bpp_id,
            "bppUri": bpp_uri,
        }
        if action == DISCOVER and self.discover_schema_context:
            context["schemaContext"] = [self.discover_schema_context]
        context.update(clean_overrides(action, overrides))
        return _compact(context)

    def callback(self, inbound_context: Mapping[str, Any], action: str | None = None) -> dict[str, Any]:
        """Echo the inbound context as the ``on_*`` callback; routing is preserved."""
        context = dict(inbound_context or {})
        context["action"] = callback_of(action or context.get("action", ""))
        if not self.reuse_message_id or not context.get("messageId"):
            context["messageId"] = new_id()
        context["timestamp"] = now_iso()
        return _compact(context)

    def unsolicited(
        self,
        action: str,
        transaction_id: str,
        routing: Mapping[str, Any],
    ) -> dict[str, Any]:
        """A BPP-initiated callback (e.g. ``on_update``) addressed with stored BAP routing."""
        return _compact(
            {
                "networkId": self.identity.network_id,
                "action": callback_of(action),
                "version": self.identity.version,
                "bapId": routing.get("bapId"),
                "bapUri": routing.get("bapUri"),
                "bppId": self.identity.bpp.id,
                "bppUri": self.identity.bpp.uri,
                "transactionId": transaction_id,
                "messageId": new_id(),
                "timestamp": now_iso(),
            }
        )

    def publish(self, transaction_id: str | None = None) -> dict[str, Any]:
        """``catalog/publish`` context: sent by the BPP, no BAP."""
        return _compact(
            {
                "networkId": self.identity.network_id,
                "action": "catalog/publish",
                "version": self.identity.version,
                "bppId": self.identity.bpp.id,
                "bppUri": self.identity.bpp.uri,
                "transactionId": transaction_id or new_id(),
                "messageId": new_id(),
                "timestamp": now_iso(),
            }
        )


def extract_routing(context: Mapping[str, Any] | None) -> dict[str, Any]:
    context = context or {}
    return {key: context.get(key) for key in ("bapId", "bapUri", "bppId", "bppUri")}
