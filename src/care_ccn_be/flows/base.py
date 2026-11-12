"""A flow is one Beckn use case: its protocol identity only. Flows are listed in ``flows.lookup.FLOWS`` and their
viewsets in ``api.viewsets.lookup.VIEWSETS``."""

from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict

from care_ccn_be.identity import identity_from_settings
from care_ccn_be.protocol.builder import CatalogBuilder
from care_ccn_be.protocol.context import ContextBuilder
from care_ccn_be.resources.beckn import BecknRequest
from care_ccn_be.settings import plugin_settings


class FlowState(BaseModel):
    """Typed view of ``BecknTransaction.state``. Each flow subclasses it; unknown keys are kept."""

    model_config = ConfigDict(extra="allow")

    v: int = 1
    context_overrides: dict[str, Any] = {}


class BecknFlow:
    name: ClassVar[str]
    aliases: ClassVar[tuple[str, ...]] = ()
    context_builder: ClassVar[type[ContextBuilder]] = ContextBuilder
    state_model: ClassVar[type[FlowState]] = FlowState
    catalog_builder: ClassVar[type[CatalogBuilder] | None] = None

    @classmethod
    def context(cls) -> ContextBuilder:
        return cls.context_builder(
            identity_from_settings(), reuse_message_id=plugin_settings.BECKN_CALLBACK_REUSE_MESSAGE_ID
        )

    @classmethod
    def match(cls, request: BecknRequest) -> int:
        """How strongly an inbound BPP request belongs to this flow (0 = not mine). Pure payload check."""
        return 0

    @classmethod
    def coordination_id(cls, request: BecknRequest) -> str:
        """Correlation id stored on the BPP transaction (referral id), if the payload carries one."""
        return ""
