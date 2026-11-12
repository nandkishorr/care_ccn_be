"""Message envelope: context, request wrapper, and the generic message bodies."""

from typing import Any, Generic, TypeVar

from care_ccn_be.resources.beckn.base import BecknModel, IsoDateTime
from care_ccn_be.resources.beckn.core import Catalog, Contract

C = TypeVar("C", bound=Contract)
M = TypeVar("M", bound=BecknModel)


class BecknContext(BecknModel):
    network_id: str | None = None
    action: str
    version: str
    bap_id: str
    bap_uri: str
    bpp_id: str | None = None
    bpp_uri: str | None = None
    transaction_id: str
    message_id: str
    timestamp: IsoDateTime
    ttl: str | None = None
    schema_context: list[str] | None = None


class CatalogContext(BecknContext):
    """``catalog/publish`` is sent by a BPP and has no BAP."""

    bap_id: str | None = None
    bap_uri: str | None = None


class ErrorBlock(BecknModel):
    type: str | None = None
    code: str | None = None
    message: str | None = None


class ContractMessage(BecknModel, Generic[C]):
    contract: C


class InboundMessage(BecknModel):
    """Flow-agnostic view of an inbound message, used before a flow is chosen."""

    contract: Contract | None = None


class CatalogFilter(BecknModel):
    """``intent.filters``: an ONIX JSONPath expression over ``$.catalogs``."""

    type: str = "jsonpath"
    expression: str | None = None


class Intent(BecknModel):
    text_search: str | None = None
    filters: CatalogFilter | None = None


class DiscoverMessage(BecknModel):
    intent: Intent = Intent()


class CatalogMessage(BecknModel):
    catalogs: list[Catalog] = []


class BecknRequest(BecknModel, Generic[M]):
    context: BecknContext
    message: M
    error: ErrorBlock | None = None


def contract_attributes(request: "BecknRequest") -> Any:
    """None-safe ``message.contract.contractAttributes`` of any request, used by ``BecknFlow.match``."""
    contract = getattr(getattr(request, "message", None), "contract", None)
    return getattr(contract, "contract_attributes", None)
