"""BAP-side reactions to the network's callbacks."""

from care_ccn_be import store
from care_ccn_be.bap.callbacks import BapCallbacksBase, CallbackCall
from care_ccn_be.flows.referral import lifecycle
from care_ccn_be.flows.referral.schema import LIFECYCLE_FULFILLED
from care_ccn_be.protocol.actions import CONTRACT_ACTIVE, CONTRACT_COMPLETED
from care_ccn_be.services import facility as facilities
from care_ccn_be.services import patient as patients


class MissingFacility(Exception):
    pass


class ReferralCallbacks(BapCallbacksBase):
    def on_init(self, call: CallbackCall) -> None:
        """The origin request is recorded as soon as the coordinator accepts the draft."""
        if call.error:
            return
        state = store.get_state(call.txn, self.flow)
        params = state.init or state.confirm
        if params and facilities.by_external_id(params.get("facility")) is not None:
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
    def _ensure_origin(call: CallbackCall, params: dict, approved: bool) -> None:
        facility = facilities.by_external_id(params.get("facility"))
        if facility is None:
            raise MissingFacility("facility is required to create the referral request")
        patient = patients.get_by_external_id(params["patient"]) if params.get("patient") else None
        data = lifecycle.RequestData(
            origin_facility=facility,
            title=params.get("title") or "Care referral",
            reason=params.get("reason") or "",
            category=params.get("category") or "other",
            emergency=params.get("emergency", False),
            priority=params.get("priority"),
            assigned_facility=facilities.by_external_id(params.get("assigned_facility_id")),
            patient=patient,
        )
        lifecycle.ensure_request(call.txn, lifecycle.ORIGIN, data, approved=approved)
