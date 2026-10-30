"""A flow is one Beckn use case. The framework only ever talks to a flow class, never to its internals."""

from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict

from care_ccn_be.identity import identity_from_settings
from care_ccn_be.protocol.builder import CatalogBuilder
from care_ccn_be.protocol.context import ContextBuilder
from care_ccn_be.protocol.schema import BecknRequest
from care_ccn_be.settings import plugin_settings


class FlowState(BaseModel):
    """Typed view of ``BecknTransaction.state``. Each flow subclasses it; unknown keys are kept."""

    model_config = ConfigDict(extra="allow")

    v: int = 1
    context_overrides: dict[str, Any] = {}


class BecknFlow:
    name: ClassVar[str]
    aliases: ClassVar[tuple[str, ...]] = ()
    legacy_service_type: ClassVar[str | None] = None
    context_builder: ClassVar[type[ContextBuilder]] = ContextBuilder
    state_model: ClassVar[type[FlowState]] = FlowState
    bap_viewset: ClassVar[str | None] = None  # dotted path; None: Care is never BAP for this flow
    bap_callbacks: ClassVar[type | None] = None  # BapCallbacksBase subclass (None: the base)
    bpp_service: ClassVar[type | None] = None  # BppServiceBase subclass; None: Care is never BPP
    catalog_builder: ClassVar[type[CatalogBuilder] | None] = None

    @classmethod
    def context(cls) -> ContextBuilder:
        return cls.context_builder(
            identity_from_settings(), reuse_message_id=plugin_settings.BECKN_CALLBACK_REUSE_MESSAGE_ID
        )

    @classmethod
    def service(cls):
        if cls.bpp_service is None:
            raise LookupError(f"flow {cls.name!r} has no BPP service")
        return cls.bpp_service(cls)

    @classmethod
    def callbacks(cls):
        from care_ccn_be.bap.callbacks import BapCallbacksBase

        return (cls.bap_callbacks or BapCallbacksBase)(cls)

    @classmethod
    def match(cls, request: BecknRequest) -> int:
        """How strongly an inbound BPP request belongs to this flow (0 = not mine). Pure payload check."""
        return 0

    @classmethod
    def coordination_id(cls, request: BecknRequest) -> str:
        """Correlation id stored on the BPP transaction (referral id), if the payload carries one."""
        return ""

    @classmethod
    def poll_extra(cls, txn) -> dict[str, Any]:
        """Flow-specific keys merged into the FE poll response."""
        return {}
