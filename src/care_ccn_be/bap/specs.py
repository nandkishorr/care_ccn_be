"""Typed request bodies of the BAP endpoints (camelCase on the wire, snake_case accepted)."""

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from care_ccn_be.models import BecknTransaction


class BapSpec(BaseModel):
    """Every POST body: optional context overrides and an optional passthrough ``message``."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    context: dict[str, Any] | None = None
    message: dict[str, Any] | None = None


class DiscoverSpec(BapSpec):
    health_service_type: str | None = None
    acceptance_mode: str | None = None
    text_search: str | None = None
    filters: dict[str, Any] | None = None
    query: dict[str, Any] | None = None  # legacy ``query`` object
    service_type: str | None = None  # legacy, the flow is in the URL now and this is ignored

    def intent_query(self) -> dict[str, Any]:
        explicit = {
            "healthServiceType": self.health_service_type,
            "acceptanceMode": self.acceptance_mode,
            "textSearch": self.text_search,
            "filters": self.filters,
        }
        return {**(self.query or {}), **{k: v for k, v in explicit.items() if v}}


class ContractRefSpec(BapSpec):
    """status: nothing but the (optional) context overrides; the contract id comes from the transaction."""


@dataclass
class BapSource:
    """What a ``bap_<action>`` builder receives: the spec, the transaction and the flow's typed state."""

    flow: type
    spec: BapSpec
    txn: BecknTransaction
    state: Any

    def __getattr__(self, name: str) -> Any:
        if name in {"flow", "spec", "txn", "state"} or name.startswith("__"):
            raise AttributeError(name)
        return getattr(self.spec, name)

    @property
    def patient(self):
        from care_ccn_be.services.patient import get_by_external_id

        return get_by_external_id(self.spec.patient)
