"""Publish a flow's catalog to the network: a ``catalog/publish`` message for the ONIX ``catalogPublish`` module."""

from dataclasses import dataclass, field
from typing import Any

from django.db import transaction

from care_ccn_be import adapter, store
from care_ccn_be.exceptions import BecknError
from care_ccn_be.models import BecknMessage

CATALOG_TYPE = "REGULAR"
UPDATE_MODE_FULL = "FULL"  # the published catalog replaces the registry's copy
UPDATE_MODE_MERGE = "MERGE"  # resources and offers are upserted into the registry's copy, by id
UPDATE_MODE = UPDATE_MODE_FULL


@dataclass
class PublishSpec:
    """Input handed to a flow's ``CatalogBuilder``."""

    public_only: bool = True
    # only these facilities (pks); a builder that scopes catalogs by facility builds one for each, even if empty
    facility_ids: list[int] | None = None


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


def build_publish_payload(
    flow, public_only: bool = True, facility_ids: list[int] | None = None, update_mode: str = UPDATE_MODE
) -> dict[str, Any]:
    catalogs = build_catalogs(flow, PublishSpec(public_only=public_only, facility_ids=facility_ids))
    context = flow.context().publish()
    visible_to = adapter.catalog_visible_to(context.get("networkId"))
    for catalog in catalogs:
        catalog.setdefault("bppId", context.get("bppId"))
        catalog.setdefault("bppUri", context.get("bppUri"))
    directives = [
        {"catalogId": catalog["id"], "catalogType": CATALOG_TYPE, "updateMode": update_mode, "visibleTo": visible_to}
        for catalog in catalogs
        if catalog.get("id")
    ]
    return {"context": context, "message": {"catalogs": catalogs, "publishDirectives": directives}}


def publish_catalog(
    flow,
    public_only: bool = True,
    dry_run: bool = False,
    facility_ids: list[int] | None = None,
    update_mode: str = UPDATE_MODE,
) -> PublishResult:
    """Record the ``catalog/publish`` message and queue its delivery; ``dry_run`` only builds the payload, and so
    does a facility-scoped publish that built no catalog (nothing to send)."""
    payload = build_publish_payload(flow, public_only, facility_ids, update_mode)
    catalogs = payload["message"]["catalogs"]
    if dry_run or (facility_ids is not None and not catalogs):
        return PublishResult(flow.name, payload, None, catalogs)
    with transaction.atomic():
        message = store.record_outbound(None, payload["context"], payload["message"])
        store.enqueue_delivery(message)
    return PublishResult(flow.name, payload, message, catalogs)
