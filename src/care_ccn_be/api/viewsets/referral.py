"""Referral APIs: BAP and BPP behavior in one flow viewset."""

from typing import Any
from uuid import UUID

from care.utils.shortcuts import get_object_or_404

from care_ccn_be import store
from care_ccn_be.api.viewsets.base import (
    BecknViewSet,
    BppCall,
    CallbackCall,
    ConfirmMixin,
    DiscoverMixin,
    InitMixin,
    SelectMixin,
    StatusMixin,
    UpdateMixin,
)
from care_ccn_be.exceptions import BecknError
from care_ccn_be.flows.defaults import cc_endpoint
from care_ccn_be.flows.referral import ReferralFlow
from care_ccn_be.models import Adapter
from care_ccn_be.models.referral import ReferralLink
from care_ccn_be.protocol.actions import CONFIRM, CONTRACT_ACTIVE, CONTRACT_COMPLETED, INIT
from care_ccn_be.resources.builders.referral import (
    CATEGORY_FOR_TYPE,
    ReferralConfirmBuilder,
    ReferralEcho,
    ReferralInitBuilder,
    ReferralRequest,
    ReferralSelectBuilder,
    referral_type_for_category,
)
from care_ccn_be.resources.referral.schema import LIFECYCLE_FULFILLED, ReferralMessage
from care_ccn_be.resources.referral.spec import (
    ReferralConfirmSpec,
    ReferralInitSpec,
    ReferralSelectSpec,
    ReferralStatusSpec,
    ReferralTransactionRetrieveSpec,
)
from care_ccn_be.services import availability as availabilities
from care_ccn_be.services import encounter as encounters
from care_ccn_be.services import facility as facilities
from care_ccn_be.services import mappers, referral_sync
from care_ccn_be.services import patient as patients
from care_ccn_be.services import referral as lifecycle
from care_ccn_be.services.patient import find_or_create_patient
from care_ccn_be.services.users import system_user

INVALID_PAYLOAD = "70002"


class MissingFacility(Exception):
    pass


class MissingPatient(Exception):
    pass


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


def contract_status(call: CallbackCall) -> str | None:
    return ((call.message.get("contract") or {}).get("status") or {}).get("code")


def origin_patient(params: dict, facility, encounter=None):
    if params.get("patient"):
        return patients.get_by_external_id(params["patient"])
    if encounter is not None:
        return encounter.patient
    if params.get("patient_name"):
        participant = {
            "id": "participant-patient",
            "descriptor": {"name": params["patient_name"]},
            "participantAttributes": {"participantRole": mappers.PARTICIPANT_ROLE_PATIENT},
        }
        return patients.find_or_create_patient({}, participant, facility, system_user())
    raise MissingPatient("a patient is required to create the referral")


NOT_FOUND = "70004"


def facility_candidates(call: BppCall) -> tuple[list, list]:
    attributes = mappers.get_contract_attributes(call.message)
    context = call.context
    origin = [context.get("facilityId"), attributes.get("facilityId"), attributes.get("originFacilityId")]
    assigned = [
        context.get("assignedFacilityId"),
        attributes.get("assignedFacilityId"),
        attributes.get("targetFacilityId"),
    ]
    return origin, assigned


class ReferralViewSet(
    DiscoverMixin,
    SelectMixin,
    InitMixin,
    ConfirmMixin,
    StatusMixin,
    UpdateMixin,
    BecknViewSet,
):
    flow = ReferralFlow
    pydantic_retrieve_model = ReferralTransactionRetrieveSpec
    contract_builder = ReferralEcho

    select_spec = ReferralSelectSpec
    init_spec = ReferralInitSpec
    confirm_spec = ReferralConfirmSpec
    status_spec = ReferralStatusSpec
    update_spec = ReferralStatusSpec  # passthrough only; Care sends its own updates from the referral signals

    on_select_spec = ReferralMessage
    on_init_spec = ReferralMessage
    on_confirm_spec = ReferralMessage
    on_status_spec = ReferralMessage
    on_update_spec = ReferralMessage
    on_discover_spec = None  # the coordination centre publishes the referral catalog, not Care

    def get_object(self):
        queryset = self.get_queryset()
        identifier = self.kwargs[self.lookup_field]
        txn = queryset.filter(transaction_id=identifier).first()
        if txn is not None:
            return txn
        if self.adapter == Adapter.BAP:
            try:
                referral_id = UUID(identifier)
            except ValueError:
                pass
            else:
                link = (
                    ReferralLink.objects.filter(
                        referral__external_id=referral_id,
                        referral__deleted=False,
                        transaction__flow=self.flow.name,
                        role=lifecycle.ORIGIN,
                        active=True,
                    )
                    .select_related("transaction")
                    .order_by("pk")
                    .first()
                )
                if link is not None:
                    coordination_id = lifecycle.coordination_of(link.transaction)
                    return get_object_or_404(queryset, coordination_id=coordination_id)
        return get_object_or_404(queryset, transaction_id=identifier)

    # ------------------------------------------------------------------ BAP
    def perform_discover(self, request_obj, txn, msg) -> None:
        store.update_transaction(txn, coordination_id=txn.transaction_id)

    def build_select(self, request_obj, txn, state):
        return ReferralSelectBuilder().message(ReferralRequest.of(request_obj, txn, state))

    def perform_select(self, request_obj: ReferralSelectSpec, txn, msg) -> None:
        if request_obj.offer_id:
            store.update_state(txn, self.flow, offer_id=request_obj.offer_id)

    def build_init(self, request_obj, txn, state):
        return ReferralInitBuilder().message(ReferralRequest.of(request_obj, txn, state))

    def authorize_init(self, request_obj: ReferralInitSpec, txn) -> None:
        self.validate_encounter(request_obj)

    def perform_init(self, request_obj: ReferralInitSpec, txn, msg) -> None:
        self.remember_params(INIT, request_obj, txn)

    def on_init(self, call: CallbackCall) -> None:
        """The origin referral is recorded as soon as the coordinator accepts the draft."""
        if call.error:
            return
        state = store.get_state(call.txn, self.flow)
        params = state.init or state.confirm
        has_patient = params and (params.get("patient") or params.get("patient_name") or params.get("encounter"))
        if has_patient and facilities.by_external_id(params.get("facility")) is not None:
            self.ensure_origin(call, params, approved=False)

    def build_confirm(self, request_obj, txn, state):
        return ReferralConfirmBuilder().message(ReferralRequest.of(request_obj, txn, state))

    def authorize_confirm(self, request_obj: ReferralConfirmSpec, txn) -> None:
        self.validate_encounter(request_obj)

    def perform_confirm(self, request_obj: ReferralConfirmSpec, txn, msg) -> None:
        self.remember_params(CONFIRM, request_obj, txn)

    def on_confirm(self, call: CallbackCall) -> None:
        if call.error or contract_status(call) != CONTRACT_ACTIVE:
            return
        state = store.get_state(call.txn, self.flow)
        params = state.confirm or state.init
        existing = lifecycle.current(lifecycle.coordination_of(call.txn), lifecycle.ORIGIN)
        if existing is not None:
            lifecycle.link_encounter(existing, encounters.by_external_id((params or {}).get("encounter")))
            lifecycle.approve(existing)
        elif params:
            existing = self.ensure_origin(call, params, approved=True)
        if existing is not None:
            link = lifecycle.current_link(lifecycle.coordination_of(call.txn), lifecycle.ORIGIN)
            if link is None:
                raise RuntimeError("confirmed origin referral has no active network link")
            lifecycle.remember_confirmation(link, call.message)
            referral_sync.schedule(existing.pk)

    def build_status(self, request_obj, txn, state):
        return {"contract": {"id": txn.contract_id or txn.transaction_id}}

    def on_status(self, call: CallbackCall) -> None:
        self.complete_if_fulfilled(call)

    def on_update(self, call: CallbackCall) -> None:
        """The receiving side's files, answered forms and status are mirrored onto the origin referral."""
        if call.error:
            return
        origin = lifecycle.current(lifecycle.coordination_of(call.txn), lifecycle.ORIGIN)
        if origin is not None:
            referral_sync.receive(origin, call.message, mirror_status=True)
        self.complete_if_fulfilled(call)

    def remember_params(self, action_name: str, request_obj, txn) -> None:
        """What ``on_init`` / ``on_confirm`` need to create the origin referral."""
        message = request_obj.message.dump() if request_obj.message is not None else None
        params = params_from_message(message) if message is not None else request_obj.params()
        if request_obj.encounter:
            params["encounter"] = str(request_obj.encounter)
        store.update_state(txn, self.flow, **{action_name: params})

    @staticmethod
    def validate_encounter(request_obj) -> None:
        """An encounter must belong to the referral's patient and origin facility."""
        if not request_obj.encounter:
            return
        facility = request_obj.facility
        if facility is None and request_obj.message is not None:
            facility = request_obj.message.contract.facility_id
        encounters.validate(request_obj.encounter, request_obj.patient, facility)

    @staticmethod
    def complete_if_fulfilled(call: CallbackCall) -> None:
        if call.error:
            return
        contract = call.message.get("contract") or {}
        lifecycle_state = (contract.get("contractAttributes") or {}).get("lifecycleState")
        if contract_status(call) == CONTRACT_COMPLETED or lifecycle_state == LIFECYCLE_FULFILLED:
            lifecycle.complete(lifecycle.coordination_of(call.txn), lifecycle.ORIGIN)

    @staticmethod
    def ensure_origin(call: CallbackCall, params: dict, approved: bool):
        facility = facilities.by_external_id(params.get("facility"))
        if facility is None:
            raise MissingFacility("facility is required to create the referral")
        encounter = encounters.by_external_id(params.get("encounter"))
        patient = origin_patient(params, facility, encounter)
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
            encounter=encounter,
        )
        return lifecycle.ensure_referral(txn, lifecycle.ORIGIN, data, approved=approved)

    # ------------------------------------------------------------------ BPP
    def perform_on_select(self, request_obj, call: BppCall) -> dict[str, Any]:
        return ReferralEcho.on_select(call.message)

    def perform_on_init(self, request_obj, call: BppCall) -> dict[str, Any]:
        origin, assigned = self._facilities(call)
        facility = origin or assigned
        if facility is None:
            raise BecknError(INVALID_PAYLOAD, "No facility id in payload; cannot create the referral")
        role = lifecycle.ORIGIN if origin else lifecycle.ASSIGNED
        referral = self._ensure(call, role, origin, assigned, approved=False)
        return ReferralEcho.on_init(call.message, referral, self._details(call, role))

    def perform_on_confirm(self, request_obj, call: BppCall) -> dict[str, Any]:
        origin, assigned = self._facilities(call)
        if origin is None and assigned is None:
            raise BecknError(INVALID_PAYLOAD, "No facility id in payload; cannot confirm the referral")
        if origin is not None:
            self._ensure(call, lifecycle.ORIGIN, origin, assigned, approved=True)
        if assigned is not None:
            self._ensure(call, lifecycle.ASSIGNED, origin, assigned, approved=True)
        link = lifecycle.current_link(lifecycle.coordination_of(call.txn))
        return ReferralEcho.on_confirm(call.message, link.referral, link.details)

    def perform_on_status(self, request_obj, call: BppCall) -> dict[str, Any]:
        link = lifecycle.current_link(lifecycle.coordination_of(call.txn))
        if link is None:
            raise BecknError(NOT_FOUND, "Referral not found for status")
        return ReferralEcho.on_status(call.message, link.referral, link.details)

    def perform_on_update(self, request_obj, call: BppCall) -> dict[str, Any]:
        """The referring side's files and answered forms are mirrored onto the assigned referral."""
        link = lifecycle.current_link(lifecycle.coordination_of(call.txn), lifecycle.ASSIGNED)
        if link is None:
            raise BecknError(NOT_FOUND, "Referral not found for update")
        referral_sync.receive(link.referral, call.message, mirror_status=False)
        return ReferralEcho.on_update(call.message, link.referral, link.details)

    @staticmethod
    def _facilities(call: BppCall):
        origin, assigned = facility_candidates(call)
        return facilities.resolve(origin), facilities.resolve(assigned)

    @staticmethod
    def _details(call: BppCall, role: str) -> dict[str, Any]:
        link = lifecycle.current_link(lifecycle.coordination_of(call.txn), role)
        return link.details if link else {}

    @staticmethod
    def _ensure(call: BppCall, role: str, origin, assigned, approved: bool):
        """Idempotent on (coordination id, role): an existing referral is reused, never duplicated.

        The origin referral sits on the network availability of the coordination centre (the referral the
        referring side raised); the assigned one sits on the receiving facility's availability.
        """
        coordination_id = lifecycle.coordination_of(call.txn)
        existing = lifecycle.current(coordination_id, role)
        if existing is not None:
            return lifecycle.approve(existing) if approved else existing
        facility = origin or assigned
        participant = mappers.find_patient_participant(call.message)
        patient = find_or_create_patient(
            mappers.get_contract_attributes(call.message), participant, facility, system_user()
        )
        if patient is None:
            raise BecknError(INVALID_PAYLOAD, "No patient participant in payload; cannot create the referral")
        referral_type = mappers.referral_type(call.message)
        if role == lifecycle.ASSIGNED:
            availability = availabilities.for_facility(assigned or facility, referral_type)
        else:
            cc = cc_endpoint()
            availability = availabilities.network(
                cc.id, cc.uri, referral_type, str(assigned.external_id) if assigned else None
            )
        data = lifecycle.ReferralData.from_message(
            call.message,
            availability,
            patient,
            origin_facility_id=str(facility.external_id),
            assignees=availabilities.assignees(availability) if role == lifecycle.ASSIGNED else (),
        )
        return lifecycle.ensure_referral(call.txn, role, data, approved=approved)
