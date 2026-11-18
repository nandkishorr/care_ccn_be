"""Referral APIs: BAP and BPP behavior in one flow viewset."""

import logging
from typing import Any
from uuid import UUID

from care.utils.shortcuts import get_object_or_404
from pydantic import ValidationError as PydanticValidationError

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
from care_ccn_be.protocol.discover import build_discover_intent
from care_ccn_be.resources.beckn.codes import CoordinationAcceptanceMode
from care_ccn_be.resources.builders.referral import (
    CATEGORY_FOR_TYPE,
    ReferralConfirmBuilder,
    ReferralEcho,
    ReferralInitBuilder,
    ReferralRequest,
    ReferralSelectBuilder,
    referral_type_for_category,
    request_form_ref,
)
from care_ccn_be.resources.referral.schema import LIFECYCLE_FULFILLED, ReferralAvailabilityBlock, ReferralMessage
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
from care_ccn_be.services import artifacts, mappers, referral_forms, referral_sync
from care_ccn_be.services import patient as patients
from care_ccn_be.services import referral as lifecycle
from care_ccn_be.services.patient import find_or_create_patient
from care_ccn_be.services.users import system_user

logger = logging.getLogger(__name__)

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


def first(values) -> str | None:
    return next((str(value) for value in values if value), None)


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
    on_discover_spec = None  # the referral catalog is published (publish_beckn_catalog), not answered on discover
    # referral units and coordination desks are ServiceCoordinationResources: no healthServiceType to filter on
    discover_filter_keys = ("acceptanceMode",)

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
    def build_discover(self, request_obj, txn, state):
        """Provider search: only resources that take referrals for review (appointment resources are ``AUTO``)."""
        query = {"acceptanceMode": CoordinationAcceptanceMode.MANUAL_REVIEW.value, **request_obj.intent_query()}
        return {"intent": build_discover_intent(query, self.discover_filter_keys)}

    def perform_discover(self, request_obj, txn, msg) -> None:
        store.update_transaction(txn, coordination_id=txn.transaction_id)

    def build_select(self, request_obj, txn, state):
        return ReferralSelectBuilder().message(ReferralRequest.of(request_obj, txn, state))

    def perform_select(self, request_obj: ReferralSelectSpec, txn, msg) -> None:
        if request_obj.offer_id:
            store.update_state(txn, self.flow, offer_id=request_obj.offer_id, availability=None)

    def on_select(self, call: CallbackCall) -> None:
        """The availability the BPP answered with, and the forms the FE renders for it."""
        if call.error:
            return
        block = (mappers.get_contract_attributes(call.message).get("referral") or {}).get("availability")
        if not isinstance(block, dict) or not block.get("id"):
            return
        try:
            availability = ReferralAvailabilityBlock.model_validate(block)
        except PydanticValidationError:
            logger.warning("ignoring an invalid referral availability in on_select of %s", call.txn.transaction_id)
            return
        stored = availability.model_dump(mode="json", by_alias=False, exclude_none=True)
        store.update_state(call.txn, self.flow, availability=stored)

    def build_init(self, request_obj, txn, state):
        return ReferralInitBuilder().message(ReferralRequest.of(request_obj, txn, state))

    def authorize_init(self, request_obj: ReferralInitSpec, txn) -> None:
        self.validate_encounter(request_obj)
        self.validate_request_form(request_obj, txn)

    def perform_init(self, request_obj: ReferralInitSpec, txn, msg) -> None:
        self.remember_params(INIT, request_obj, txn)

    def build_confirm(self, request_obj, txn, state):
        return ReferralConfirmBuilder().message(ReferralRequest.of(request_obj, txn, state))

    def authorize_confirm(self, request_obj: ReferralConfirmSpec, txn) -> None:
        self.validate_encounter(request_obj)
        self.validate_request_form(request_obj, txn)

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
            self.save_sent_form(call, existing, params or {})
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

    def validate_request_form(self, request_obj, txn) -> None:
        """The request form of the selected availability must be answered; the BPP validates the answers."""
        if request_obj.message is not None:
            return
        state = store.get_state(txn, self.flow)
        if not state.availability:
            if request_obj.questionnaire:
                raise BecknError(INVALID_PAYLOAD, "Select a referral availability before answering its questionnaire")
            return
        availability = ReferralAvailabilityBlock.model_validate(state.availability)
        if not request_obj.questionnaire:
            if availability.of_type(availabilities.REQUEST):
                raise BecknError(
                    referral_forms.FORM_REQUIRED, f"The request questionnaire of {availability.name!r} must be answered"
                )
            return
        if availability.questionnaire(request_obj.questionnaire, availabilities.REQUEST) is None:
            raise BecknError(
                referral_forms.FORM_INVALID, "questionnaire is not a request questionnaire of the selected availability"
            )
        from care.emr.resources.questionnaire_response.spec import QuestionnaireResponseSubmission

        try:
            QuestionnaireResponseSubmission.model_validate(request_obj.questionnaire_response or {})
        except PydanticValidationError as exc:
            reasons = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:5])
            raise BecknError(referral_forms.FORM_INVALID, f"questionnaire_response: {reasons}") from exc

    @staticmethod
    def remote_availability(txn) -> ReferralAvailabilityBlock | None:
        data = (txn.state or {}).get("availability")
        return ReferralAvailabilityBlock.model_validate(data) if data else None

    @staticmethod
    def save_sent_form(call: CallbackCall, referral, params: dict) -> None:
        """Keep the request form the BPP accepted on the origin referral."""
        availability = ReferralViewSet.remote_availability(call.txn)
        questionnaire_id = params.get("questionnaire")
        if availability is None or not questionnaire_id:
            return
        snapshot = availability.questionnaire(questionnaire_id, availabilities.REQUEST)
        if snapshot is None:
            return
        ref = request_form_ref(lifecycle.coordination_of(call.txn))
        referral_forms.save_sent(referral, ref, snapshot, params.get("questionnaire_response") or {})

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
        remote = ReferralViewSet.remote_availability(txn)
        if remote is None:
            availability = availabilities.network(
                txn.counterparty_id,
                txn.counterparty_uri,
                referral_type_for_category(params.get("category")),
                params.get("assigned_facility_id"),
            )
        else:
            name = " - ".join(filter(None, [remote.name, remote.facility_name]))
            availability = availabilities.network(
                txn.counterparty_id,
                txn.counterparty_uri,
                remote.referral_type or referral_type_for_category(params.get("category")),
                params.get("assigned_facility_id") or remote.facility_id,
                name=name or None,
                availability_id=remote.id,
            )
            availabilities.link_questionnaires(availability, remote)
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
        """A selected catalog offer is answered with its availability and the forms it asks for."""
        _, claimed = facility_candidates(call)
        availability = referral_forms.resolve(
            call.message, first(claimed), None, mappers.referral_type(call.message), from_offer=True
        )
        return ReferralEcho.on_select(call.message, availabilities.block(availability) if availability else None)

    def perform_on_init(self, request_obj, call: BppCall) -> dict[str, Any]:
        """Init only validates: the receiving availability and its request form. Referrals are created on confirm."""
        origin, assigned, availability = self._receiving(call)
        if origin is None and assigned is None:
            raise BecknError(INVALID_PAYLOAD, "No facility id in payload; cannot create the referral")
        if mappers.find_patient_participant(call.message) is None:
            raise BecknError(INVALID_PAYLOAD, "No patient participant in payload; cannot create the referral")
        if availability is not None:
            referral_forms.request_form(call.message, availability)
        block = availabilities.block(availability, with_questionnaires=False) if availability else None
        return ReferralEcho.validated(call.message, block)

    def perform_on_confirm(self, request_obj, call: BppCall) -> dict[str, Any]:
        coordination_id = lifecycle.coordination_of(call.txn)
        existing = lifecycle.current(coordination_id, lifecycle.ASSIGNED)
        if existing is not None:
            origin, _ = self._facilities(call)
            availability = existing.availability
            assigned, form = availabilities.facility_of(availability), None
        else:
            origin, assigned, availability = self._receiving(call)
            form = referral_forms.request_form(call.message, availability) if availability else None
        if origin is None and assigned is None:
            raise BecknError(INVALID_PAYLOAD, "No facility id in payload; cannot confirm the referral")
        if origin is not None:
            self._ensure(call, lifecycle.ORIGIN, origin, assigned, approved=True)
        if assigned is not None:
            referral = self._ensure(
                call, lifecycle.ASSIGNED, origin, assigned, approved=True, availability=availability
            )
            if form is not None:
                referral_forms.save(form, referral)
        link = lifecycle.current_link(coordination_id)
        return ReferralEcho.on_confirm(call.message, link.referral, link.details)

    def perform_on_status(self, request_obj, call: BppCall) -> dict[str, Any]:
        link = lifecycle.current_link(lifecycle.coordination_of(call.txn))
        if link is None:
            raise BecknError(NOT_FOUND, "Referral not found for status")
        return ReferralEcho.on_status(call.message, link.referral, link.details)

    def perform_on_update(self, request_obj, call: BppCall) -> dict[str, Any]:
        """The referring side's files and answered forms are mirrored onto the assigned referral. An update carrying
        a document that could never be downloaded is rejected as a whole, so the sender sees it in ``on_update``."""
        link = lifecycle.current_link(lifecycle.coordination_of(call.txn), lifecycle.ASSIGNED)
        if link is None:
            raise BecknError(NOT_FOUND, "Referral not found for update")
        try:
            artifacts.check_incoming(link.referral, call.message)
        except artifacts.ArtifactRejected as exc:
            raise BecknError(INVALID_PAYLOAD, f"Referral update rejected: {exc}") from exc
        referral_sync.receive(link.referral, call.message, mirror_status=False)
        return ReferralEcho.on_update(call.message, link.referral, link.details)

    @staticmethod
    def _facilities(call: BppCall):
        origin, assigned = facility_candidates(call)
        return facilities.resolve(origin), facilities.resolve(assigned)

    @staticmethod
    def _receiving(call: BppCall):
        """(origin facility, receiving facility, its availability); a named availability fixes the facility."""
        origin_ids, assigned_ids = facility_candidates(call)
        origin, assigned = facilities.resolve(origin_ids), facilities.resolve(assigned_ids)
        availability = referral_forms.resolve(
            call.message, first(assigned_ids), assigned, mappers.referral_type(call.message)
        )
        if availability is not None:
            assigned = availabilities.facility_of(availability)
        return origin, assigned, availability

    @staticmethod
    def _ensure(call: BppCall, role: str, origin, assigned, approved: bool, availability=None):
        """Idempotent on (coordination id, role): an existing referral is reused, never duplicated.

        The origin referral sits on the network availability of the coordination centre (the referral the
        referring side raised); the assigned one sits on the receiving availability.
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
        if role == lifecycle.ORIGIN:
            cc = cc_endpoint()
            availability = availabilities.network(
                cc.id, cc.uri, mappers.referral_type(call.message), str(assigned.external_id) if assigned else None
            )
        data = lifecycle.ReferralData.from_message(
            call.message,
            availability,
            patient,
            origin_facility_id=str(facility.external_id),
            assignees=availabilities.assignees(availability) if role == lifecycle.ASSIGNED else (),
        )
        return lifecycle.ensure_referral(call.txn, role, data, approved=approved)
