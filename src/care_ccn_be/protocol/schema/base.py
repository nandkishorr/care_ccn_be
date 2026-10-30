"""Base classes for every Beckn object and the JSON-LD ``*Attributes`` extension slots."""

from datetime import datetime
from typing import Annotated, Any, ClassVar

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    SerializeAsAny,
    model_validator,
)
from pydantic.alias_generators import to_camel

# Serialised like Care does today: ``datetime.isoformat`` ("+00:00", not "Z").
IsoDateTime = Annotated[AwareDatetime, PlainSerializer(datetime.isoformat, return_type=str, when_used="json")]


class BecknModel(BaseModel):
    """snake_case in Python, camelCase on the wire; unknown fields are kept and echoed."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        validate_by_name=True,
        validate_by_alias=True,
        serialize_by_alias=True,
        extra="allow",
    )

    def dump(self) -> dict[str, Any]:
        """Wire JSON: only what was received or explicitly built."""
        return self.model_dump(mode="json", exclude_unset=True, exclude_none=True)


class AttributeRegistry:
    def __init__(self) -> None:
        self._models: dict[tuple[str, str | None], type["JsonLdModel"]] = {}  # noqa: UP037

    def register(self, model: type["JsonLdModel"]) -> type["JsonLdModel"]:  # noqa: UP037
        for ctx in model.LD_CONTEXTS or (None,):
            self._models[(model.LD_TYPE, ctx)] = model
        return model

    def resolve(self, value: Any) -> Any:
        if not isinstance(value, dict):
            return value  # already a model instance (outbound builders)
        ld_type, ctx = value.get("@type"), value.get("@context")
        ctx = ctx if isinstance(ctx, str) else None
        model = self._models.get((ld_type, ctx)) or self._models.get((ld_type, None)) or JsonLdModel
        return model.model_validate(value)


ATTRIBUTES = AttributeRegistry()


class JsonLdModel(BecknModel):
    """Generic ``*Attributes`` object. Subclasses that declare ``LD_TYPE`` register themselves by ``@type``."""

    LD_TYPE: ClassVar[str | None] = None
    LD_CONTEXT: ClassVar[str | None] = None  # stamped on outbound objects
    LD_CONTEXTS: ClassVar[tuple[str, ...]] = ()  # pin to exact @context versions (empty = any)

    ld_context: str | list[str] | None = Field(default=None, alias="@context")
    ld_type: str | None = Field(default=None, alias="@type")

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        super().__pydantic_init_subclass__(**kwargs)
        if cls.LD_TYPE and {"LD_TYPE", "LD_CONTEXTS"} & cls.__dict__.keys():
            ATTRIBUTES.register(cls)

    @model_validator(mode="after")
    def _stamp_jsonld(self):
        if self.ld_type is None and self.LD_TYPE:
            self.ld_type = self.LD_TYPE
        if self.ld_context is None and self.LD_CONTEXT:
            self.ld_context = self.LD_CONTEXT
        return self


Attributes = Annotated[SerializeAsAny[JsonLdModel], BeforeValidator(ATTRIBUTES.resolve)]
