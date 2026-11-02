"""BAP-side reactions to the network's callbacks."""

from care_ccn_be import store
from care_ccn_be.bap.callbacks import BapCallbacksBase, CallbackCall
from care_ccn_be.flows.referral import lifecycle
from care_ccn_be.flows.referral.builders import referral_type_for_category
from care_ccn_be.flows.referral.schema import LIFECYCLE_FULFILLED
from care_ccn_be.protocol.actions import CONTRACT_ACTIVE, CONTRACT_COMPLETED
from care_ccn_be.services import availability as availabilities
from care_ccn_be.services import facility as facilities
from care_ccn_be.services import mappers
from care_ccn_be.services import patient as patients
from care_ccn_be.services.users import system_user


class MissingFacility(Exception):
    pass


class MissingPatient(Exception):
    pass


class ReferralCallbacks(BapCallbacksBase):
    def on_init(self, call: CallbackCall) -> None:
        """The origin referral is recorded as soon as the coordinator accepts the draft."""
        if call.error:
            return
        state = store.get_state(call.txn, self.flow)
        params = state.init or state.confirm
        has_patient = params and (params.get("patient") or params.get("patient_name"))
        if has_patient and facilities.by_external_id(params.get("facility")) is not None:
            self._ensure_origin(call, params, approved=False)

    def on_confirm(self, call: CallbackCall) -> None:
        if call.error or self._status(call) != CONTRACT_ACTIVE:
            return
        state = store.get_state(call.txn, self.flow)
        coordination_id = lifecycle.coordination_of(call.txn)
        existing = lifecycle.current(coordination_id, lifecycle.ORIGIN)
        if existing is not None:
            lifecycle.approve(existing)
            return
        params = state.confirm or state.init
        if params:
            self._ensure_origin(call, params, approved=True)

    def on_update(self, call: CallbackCall) -> None:
        self._complete_if_fulfilled(call)

    def on_status(self, call: CallbackCall) -> None:
        self._complete_if_fulfilled(call)

    def _complete_if_fulfilled(self, call: CallbackCall) -> None:
        if call.error:
            return
        contract = call.message.get("contract") or {}
        lifecycle_state = (contract.get("contractAttributes") or {}).get("lifecycleState")
        if self._status(call) == CONTRACT_COMPLETED or lifecycle_state == LIFECYCLE_FULFILLED:
            lifecycle.complete(lifecycle.coordination_of(call.txn), lifecycle.ORIGIN)

    @staticmethod
    def _status(call: CallbackCall) -> str | None:
        return ((call.message.get("contract") or {}).get("status") or {}).get("code")

    @staticmethod
    def _patient(params: dict, facility):
        if params.get("patient"):
            return patients.get_by_external_id(params["patient"])
        if params.get("patient_name"):
            participant = {
                "id": "participant-patient",
                "descriptor": {"name": params["patient_name"]},
                "participantAttributes": {"participantRole": mappers.PARTICIPANT_ROLE_PATIENT},
            }
            return patients.find_or_create_patient({}, participant, facility, system_user())
        raise MissingPatient("a patient is required to create the referral")

    @staticmethod
    def _ensure_origin(call: CallbackCall, params: dict, approved: bool) -> None:
        facility = facilities.by_external_id(params.get("facility"))
        if facility is None:
            raise MissingFacility("facility is required to create the referral")
        patient = ReferralCallbacks._patient(params, facility)
        txn = call.txn
        availability = availabilities.network(
            txn.counterparty_id,
            txn.counterparty_uri,
            referral_type_for_category(params.get("category")),
            params.get("assigned_facility_id"),
        )
        data = lifecycle.ReferralData(
            patient=patient,
            availability=availability,
            title=params.get("title") or "Care referral",
            note=params.get("reason") or "",
            urgency=mappers.urgency_tier(params.get("emergency", False), params.get("priority")),
            origin_facility_id=str(facility.external_id),
        )
        lifecycle.ensure_referral(txn, lifecycle.ORIGIN, data, approved=approved)
