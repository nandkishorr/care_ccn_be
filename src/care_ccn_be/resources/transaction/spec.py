"""EMR read specs of a ``BecknTransaction``: what the list and retrieve APIs return."""

import datetime
from typing import Any

from care.emr.resources.base import EMRResource, model_from_cache
from pydantic import UUID4
from pydantic.alias_generators import to_snake

from care_ccn_be import store
from care_ccn_be.identity import identity_from_settings
from care_ccn_be.models import Adapter, BecknTransaction


class BecknTransactionBaseSpec(EMRResource):
    __model__ = BecknTransaction
    __exclude__ = ["state", "status_message_id", "deleted"]

    id: UUID4 | None = None


class BecknTransactionListSpec(BecknTransactionBaseSpec):
    transaction_id: str = ""
    adapter: str = ""
    flow: str = ""
    status: str = ""
    network_id: str = ""
    counterparty_id: str = ""
    counterparty_uri: str = ""
    contract_id: str = ""
    coordination_id: str = ""
    last_error: dict[str, Any] | None = None
    created_date: datetime.datetime | None = None
    modified_date: datetime.datetime | None = None


class BecknTransactionRetrieveSpec(BecknTransactionListSpec):
    routing: dict[str, str] = {}
    context: dict[str, Any] = {}
    actions: list[str] = []
    patient: UUID4 | None = None
    facility: UUID4 | None = None
    created_by: dict | None = None

    @classmethod
    def perform_extra_serialization(cls, mapping, obj, *args, **kwargs):
        from care.emr.resources.user.spec import UserSpec

        super().perform_extra_serialization(mapping, obj, *args, **kwargs)
        mapping["routing"] = routing_of(obj)
        overrides = (obj.state or {}).get("context_overrides") or {}
        mapping["context"] = {to_snake(key): value for key, value in overrides.items()}
        mapping["actions"] = store.actions_seen(obj)
        mapping["patient"] = obj.patient.external_id if obj.patient_id else None
        mapping["facility"] = obj.facility.external_id if obj.facility_id else None
        if obj.created_by_id:
            mapping["created_by"] = model_from_cache(UserSpec, id=obj.created_by_id)


def routing_of(txn: BecknTransaction) -> dict[str, str]:
    """Both ends of the conversation: this Care instance on its side, the counterparty on the other."""
    identity = identity_from_settings()
    if txn.adapter == Adapter.BAP:
        return {
            "bap_id": identity.bap.id,
            "bap_uri": identity.bap.uri,
            "bpp_id": txn.counterparty_id,
            "bpp_uri": txn.counterparty_uri,
        }
    return {
        "bap_id": txn.counterparty_id,
        "bap_uri": txn.counterparty_uri,
        "bpp_id": identity.bpp.id,
        "bpp_uri": identity.bpp.uri,
    }
