"""Publish a flow's catalog to the network: a ``catalog/publish`` message for the ONIX ``catalogPublish`` module."""

from dataclasses import dataclass, field
from typing import Any

from django.db import transaction

from care_ccn_be import store
from care_ccn_be.exceptions import BecknError
from care_ccn_be.models import BecknMessage

CATALOG_TYPE = "REGULAR"
UPDATE_MODE = "FULL"


@dataclass
class PublishSpec:
    """Input handed to a flow's ``CatalogBuilder``."""

    public_only: bool = True


@dataclass
class PublishResult:
    flow: str
    payload: dict[str, Any]
    message: BecknMessage | None = None
    catalogs: list[dict[str, Any]] = field(default_factory=list)

    @property
    def dry_run(self) -> bool:
        return self.message is None


def build_catalogs(flow, spec: PublishSpec) -> list[dict[str, Any]]:
    if flow.catalog_builder is None:
        raise BecknError("70003", f"flow {flow.name!r} publishes no catalog")
    builder = flow.catalog_builder()
    catalogs = builder.catalogs(spec) if hasattr(builder, "catalogs") else [builder.build(spec)]
    return [catalog for catalog in catalogs if catalog]


def build_publish_payload(flow, public_only: bool = True) -> dict[str, Any]:
    catalogs = build_catalogs(flow, PublishSpec(public_only=public_only))
    context = flow.context().publish()
    visible_to = [context["networkId"]] if context.get("networkId") else []
    for catalog in catalogs:
        catalog.setdefault("bppId", context.get("bppId"))
        catalog.setdefault("bppUri", context.get("bppUri"))
    directives = [
        {"catalogId": catalog["id"], "catalogType": CATALOG_TYPE, "updateMode": UPDATE_MODE, "visibleTo": visible_to}
        for catalog in catalogs
        if catalog.get("id")
    ]
    return {"context": context, "message": {"catalogs": catalogs, "publishDirectives": directives}}


def publish_catalog(flow, public_only: bool = True, dry_run: bool = False) -> PublishResult:
    """Record the ``catalog/publish`` message and queue its delivery; ``dry_run`` only builds the payload."""
    payload = build_publish_payload(flow, public_only)
    catalogs = payload["message"]["catalogs"]
    if dry_run:
        return PublishResult(flow.name, payload, None, catalogs)
    with transaction.atomic():
        message = store.record_outbound(None, payload["context"], payload["message"])
        store.enqueue_delivery(message)
    return PublishResult(flow.name, payload, message, catalogs)
