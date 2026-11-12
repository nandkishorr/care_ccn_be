"""Request and response specs shared by every BAP action API (snake_case, like CARE EMR)."""

from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, model_validator
from pydantic.alias_generators import to_camel


class BapContextSpec(BaseModel):
    """Context overrides remembered for later actions. Unknown keys are ignored."""

    model_config = ConfigDict(extra="ignore")

    network_id: str
    version: str | None = None
    bap_id: str
    bap_uri: str
    bpp_id: str
    bpp_uri: str
    ttl: str | None = None
    schema_context: list[str] | None = None

    def to_wire(self) -> dict[str, Any]:
        return {to_camel(key): value for key, value in self.model_dump(exclude_none=True).items()}


class BapActionSpec(BaseModel):
    """Every BAP action body: optional context overrides and an optional raw Beckn ``message`` (passthrough).

    ``__required__`` lists the fields a subclass needs when no passthrough ``message`` is sent.
    """

    model_config = ConfigDict(extra="forbid")

    __required__: ClassVar[tuple[str, ...]] = ()

    context: BapContextSpec | None = None
    message: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _required_without_passthrough(self):
        if self.message is None:
            missing = [name for name in self.__required__ if getattr(self, name) in (None, "")]
            if missing:
                raise ValueError(f"{', '.join(missing)} {'is' if len(missing) == 1 else 'are'} required")
        return self

    def context_overrides(self) -> dict[str, Any] | None:
        return self.context.to_wire() if self.context else None

    def params(self) -> dict[str, Any]:
        """The request fields (no context / message), kept in the transaction state for the callbacks."""
        return self.model_dump(mode="json", exclude={"context", "message"}, exclude_none=True)


class BapDiscoverSpec(BapActionSpec):
    health_service_type: str | None = None
    acceptance_mode: str | None = None
    text_search: str | None = None
    filters: dict[str, Any] | None = None

    def intent_query(self) -> dict[str, Any]:
        query = {
            "healthServiceType": self.health_service_type,
            "acceptanceMode": self.acceptance_mode,
            "textSearch": self.text_search,
            "filters": self.filters,
        }
        return {key: value for key, value in query.items() if value}


class BapActionResponseSpec(BaseModel):
    """``202``: the request is stored and queued to the ONIX BAP caller; poll the transaction for the callback."""

    transaction_id: str
    message_id: str
    action: str
    status: str


class BapCatalogsSpec(BaseModel):
    transaction_id: str
    catalogs: list[dict[str, Any]] = []
