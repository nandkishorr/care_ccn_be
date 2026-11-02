from typing import Any

from care_ccn_be import store
from care_ccn_be.bap.mixins import (
    BapConfirmMixin,
    BapDiscoverMixin,
    BapInitMixin,
    BapSelectMixin,
    BapStatusMixin,
)
from care_ccn_be.bap.specs import BapSource, ContractRefSpec
from care_ccn_be.bap.viewset import BapViewSetBase
from care_ccn_be.exceptions import BecknError
from care_ccn_be.flows.referral.builders import (
    CATEGORY_FOR_TYPE,
    ReferralConfirmBuilder,
    ReferralInitBuilder,
    ReferralSelectBuilder,
)
from care_ccn_be.flows.referral.flow import ReferralFlow
from care_ccn_be.flows.referral.specs import (
    FACILITY_REQUIRED,
    OFFER_REQUIRED,
    ReferralConfirmSpec,
    ReferralInitSpec,
    ReferralSelectSpec,
)
from care_ccn_be.protocol.actions import CONFIRM, DISCOVER, INIT, SELECT, STATUS
from care_ccn_be.protocol.schema import Contract
from care_ccn_be.services import mappers

INVALID_PAYLOAD = "70002"


def params_from_message(message: dict[str, Any]) -> dict[str, Any]:
    """Request parameters recovered from a passthrough contract so ``on_confirm`` can still create the origin."""
    contract = mappers.get_contract(message)
    attributes = contract.get("contractAttributes") or {}
    descriptor = contract.get("descriptor") or {}
    participant = mappers.find_patient_participant(message) or {}
    params = {
        "patient_name": (participant.get("descriptor") or {}).get("name"),
        "category": CATEGORY_FOR_TYPE.get(mappers.referral_type(message)),
        "facility": attributes.get("facilityId") or attributes.get("originFacilityId"),
        "assigned_facility_id": attributes.get("assignedFacilityId"),
        "title": descriptor.get("name"),
        "reason": descriptor.get("shortDesc"),
    }
    return {key: value for key, value in params.items() if value}


def offer_id_of(message: dict[str, Any]) -> str | None:
    contract = mappers.get_contract(message)
    for entry in [
        *(contract.get("offers") or []),
        *((c or {}).get("offer") or {} for c in contract.get("commitments") or []),
    ]:
        if (entry or {}).get("id"):
            return entry["id"]
    return None


class ReferralBapViewSet(
    BapDiscoverMixin, BapSelectMixin, BapInitMixin, BapConfirmMixin, BapStatusMixin, BapViewSetBase
):
    flow = ReferralFlow
    spec_models = {
        **BapViewSetBase.spec_models,
        SELECT: ReferralSelectSpec,
        INIT: ReferralInitSpec,
        CONFIRM: ReferralConfirmSpec,
        STATUS: ContractRefSpec,
    }

    def validate_passthrough(self, action: str, message: dict[str, Any]) -> dict[str, Any]:
        try:
            Contract.model_validate(mappers.get_contract(message))
        except ValueError as exc:
            raise BecknError(INVALID_PAYLOAD, f"invalid contract: {exc}") from exc
        attributes = mappers.get_contract_attributes(message)
        if action == SELECT and not offer_id_of(message):
            raise BecknError(INVALID_PAYLOAD, OFFER_REQUIRED)
        if action == CONFIRM and not (attributes.get("facilityId") or attributes.get("originFacilityId")):
            raise BecknError(INVALID_PAYLOAD, FACILITY_REQUIRED)
        return message

    def bap_select(self, source: BapSource):
        return ReferralSelectBuilder().message(source)

    def bap_init(self, source: BapSource):
        return ReferralInitBuilder().message(source)

    def bap_confirm(self, source: BapSource):
        return ReferralConfirmBuilder().message(source)

    def bap_status(self, source: BapSource):
        return {"contract": {"id": source.txn.contract_id or source.txn.transaction_id}}

    def after_send(self, action: str, source: BapSource, msg) -> None:
        txn, flow, spec = source.txn, self.flow, source.spec
        if action == DISCOVER:
            store.update_transaction(txn, coordination_id=txn.transaction_id)
        elif action == SELECT and getattr(spec, "offer_id", None):
            store.update_state(txn, flow, offer_id=spec.offer_id)
        elif action in (INIT, CONFIRM):
            params = params_from_message(spec.message) if spec.message is not None else spec.params()
            if spec.message is None and getattr(spec, "patient_name", None):
                params["patient_name"] = spec.patient_name
            store.update_state(txn, flow, **{action: params})
