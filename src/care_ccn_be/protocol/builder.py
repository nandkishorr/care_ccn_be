"""Composable builders for outbound contracts: one ``Part`` per contract field."""

import copy
from typing import Any, ClassVar

from care_ccn_be.resources.beckn.core import Contract
from care_ccn_be.resources.beckn.envelope import ContractMessage


class Part:
    """One piece of an outbound contract. Grow a payload by appending a Part to ``parts``."""

    field: ClassVar[str]

    def build(self, spec: Any, builder: "ContractBuilder") -> Any:
        raise NotImplementedError


class ContractBuilder:
    contract_model: ClassVar[type[Contract]] = Contract
    status_code: ClassVar[str] = "ACTIVE"
    parts: ClassVar[tuple[type[Part], ...]] = ()

    def build(self, spec: Any) -> Contract:
        values = {}
        for part in self.parts:
            value = part().build(spec, self)
            if value is not None:
                values[part.field] = value
        return self.contract_model(**values)

    def message(self, spec: Any) -> dict[str, Any]:
        return {"contract": self.build(spec).dump()}

    @classmethod
    def echo(cls, inbound: ContractMessage | dict[str, Any], status_code: str | None = None) -> dict[str, Any]:
        """Echo an inbound contract back unchanged (extra fields kept), optionally setting the status."""
        body = inbound.dump() if hasattr(inbound, "dump") else copy.deepcopy(dict(inbound or {}))
        contract = body.setdefault("contract", {})
        code = status_code or cls.status_code
        if code:
            contract.setdefault("status", {})["code"] = code
        return body

    @classmethod
    def inbound(cls, message: dict[str, Any]) -> Contract:
        return cls.contract_model.model_validate((message or {}).get("contract") or {})


class CatalogBuilder:
    """Assembles ``catalogs`` for discover callbacks and ``catalog/publish``."""

    def build(self, spec: Any) -> dict[str, Any]:
        raise NotImplementedError
