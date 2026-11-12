"""Appointment APIs: BAP and BPP behavior in one flow viewset."""

from datetime import UTC, date, datetime, time
from typing import Any

from care.emr.models import Patient
from care.utils.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import ValidationError as DrfValidationError

from care_ccn_be import store
from care_ccn_be.api.viewsets.base import (
    BecknViewSet,
    BppCall,
    CallbackCall,
    CancelMixin,
    ConfirmMixin,
    DiscoverMixin,
    InitMixin,
    SelectMixin,
    StatusMixin,
    UpdateMixin,
)
from care_ccn_be.constants import (
    ACCEPTANCE_MODE_MANUAL_REVIEW,
    CANCELLED_BOOKING_STATUSES,
    DEFAULT_SERVICE_TYPE,
)
from care_ccn_be.context_vars import suppress_beckn_signals
from care_ccn_be.exceptions import BecknError
from care_ccn_be.flows.appointment import AppointmentFlow
from care_ccn_be.models.appointment import AppointmentLink
from care_ccn_be.resources.appointment.schema import (
    AppointmentConfirmMessage,
    AppointmentContract,
    AppointmentInitMessage,
    AppointmentMessage,
    AppointmentSelectMessage,
)
from care_ccn_be.resources.appointment.spec import (
    AppointmentCancelSpec,
    AppointmentConfirmSpec,
    AppointmentInitSpec,
    AppointmentRescheduleSpec,
    AppointmentSelectSpec,
    AppointmentStatusSpec,
    AppointmentTransactionRetrieveSpec,
)
from care_ccn_be.resources.builders.appointment import (
    AppointmentCancelBuilder,
    AppointmentConfirmBuilder,
    AppointmentContractBuilder,
    AppointmentInitBuilder,
    AppointmentSelectBuilder,
    AppointmentStatusBuilder,
    AppointmentUpdateBuilder,
    BookingRequest,
    echo_with_booking,
    echo_with_slots,
    service_type_of,
)
from care_ccn_be.resources.catalog.appointment import acceptance_mode, health_service_type
from care_ccn_be.services import scheduling
from care_ccn_be.services.patient import appointment_patient_info, find_or_create_appointment_patient

INVALID_PAYLOAD = "70002"


def day_window(day) -> tuple[datetime, datetime]:
    return datetime.combine(day, time.min, tzinfo=UTC), datetime.combine(day, time(23, 59, 59), tzinfo=UTC)


def catalog_service_type(txn, resource_id: str) -> str | None:
    for catalog in store.merged_catalogs(txn):
        for resource in catalog.get("resources") or []:
            if resource.get("id") == resource_id:
                return (resource.get("resourceAttributes") or {}).get("healthServiceType")
    return None


def parse_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def contract_id_of(txn) -> str:
    if not txn.contract_id:
        raise BecknError(INVALID_PAYLOAD, "the appointment is not confirmed yet")
    return txn.contract_id


def booking_request(state, **values) -> BookingRequest:
    """A request on the selected resource; a ``slot_id`` carries the window offered in ``on_select``."""
    if not state.resource_id:
        raise BecknError(INVALID_PAYLOAD, "select a resource before this step")
    slot = state.slots.get(values.get("slot_id") or "", {})
    return BookingRequest(
        resource_id=state.resource_id,
        offer_id=state.offer_id,
        service_type=state.service_type or DEFAULT_SERVICE_TYPE,
        window_start=parse_iso(slot.get("start")),
        window_end=parse_iso(slot.get("end")),
        **values,
    )


def patient_info(request_obj):
    return appointment_patient_info(get_object_or_404(Patient, external_id=request_obj.patient))


NOT_FOUND = "70004"
UNAVAILABLE = "70005"


def _drf_text(error: DrfValidationError) -> str:
    detail = error.detail
    if isinstance(detail, list | tuple) and detail:
        return str(detail[0])
    return str(detail)


def resource_of(contract: AppointmentContract):
    resource = scheduling.get_resource(contract.resource_id)
    if resource is None:
        raise BecknError(NOT_FOUND, f"unknown resource {contract.resource_id!r}")
    return resource


def slot_of(contract: AppointmentContract, slot_id: str | None = None):
    slot = scheduling.get_slot(slot_id or contract.slot_id)
    if slot is None:
        raise BecknError(NOT_FOUND, f"unknown slot {slot_id or contract.slot_id!r}")
    if contract.resource_id and str(slot.resource.external_id) != contract.resource_id:
        raise BecknError(INVALID_PAYLOAD, "the slot does not belong to the selected resource")
    return slot


def requested_day(message: AppointmentSelectMessage) -> date:
    if message.intent is not None and message.intent.day is not None:
        return message.intent.day
    start, _ = message.contract.slot_window
    if start is not None:
        return start.astimezone(UTC).date()
    return timezone.localdate()


def booking_of(call: BppCall):
    link = (
        AppointmentLink.objects.select_related("token_booking", "token_booking__token_slot")
        .filter(transaction=call.txn, active=True)
        .first()
    )
    if link is not None:
        return link.token_booking
    booking = scheduling.find_booking(((call.message.get("contract") or {}).get("id")) or call.txn.contract_id)
    if booking is None:
        raise BecknError(NOT_FOUND, "appointment not found")
    return booking


def attach(call: BppCall, booking, patient, facility, coordination_ref: str = "") -> None:
    AppointmentLink.objects.filter(transaction=call.txn, active=True).update(active=False)
    AppointmentLink.objects.create(
        transaction=call.txn, token_booking=booking, beckn_ref=str(booking.external_id), active=True
    )
    fields: dict[str, Any] = {"contract_id": str(booking.external_id), "patient": patient, "facility": facility}
    if coordination_ref:
        fields["coordination_id"] = coordination_ref
    store.update_transaction(call.txn, **fields)


class AppointmentViewSet(
    DiscoverMixin,
    SelectMixin,
    InitMixin,
    ConfirmMixin,
    StatusMixin,
    UpdateMixin,
    CancelMixin,
    BecknViewSet,
):
    flow = AppointmentFlow
    pydantic_retrieve_model = AppointmentTransactionRetrieveSpec
    contract_builder = AppointmentContractBuilder

    select_spec = AppointmentSelectSpec
    init_spec = AppointmentInitSpec
    confirm_spec = AppointmentConfirmSpec
    status_spec = AppointmentStatusSpec
    update_spec = AppointmentRescheduleSpec
    cancel_spec = AppointmentCancelSpec

    on_select_spec = AppointmentSelectMessage
    on_init_spec = AppointmentInitMessage
    on_confirm_spec = AppointmentConfirmMessage
    on_update_spec = AppointmentMessage
    on_cancel_spec = AppointmentMessage

    # ------------------------------------------------------------------ BAP
    def build_select(self, request_obj: AppointmentSelectSpec, txn, state):
        state = store.update_state(
            txn,
            self.flow,
            resource_id=request_obj.resource_id,
            offer_id=request_obj.offer_id or f"offer-{request_obj.resource_id}",
            service_type=request_obj.health_service_type
            or catalog_service_type(txn, request_obj.resource_id)
            or state.service_type
            or DEFAULT_SERVICE_TYPE,
        )
        window_start, window_end = day_window(request_obj.date) if request_obj.date else (None, None)
        request = BookingRequest(
            resource_id=state.resource_id,
            offer_id=state.offer_id,
            service_type=state.service_type,
            window_start=window_start,
            window_end=window_end,
        )
        return AppointmentSelectBuilder().message(request)

    def on_select(self, call: CallbackCall) -> None:
        """Remember the offered slots so init/confirm/update can carry their windows."""
        slots = {}
        for performance in (call.message.get("contract") or {}).get("performance") or []:
            attributes = performance.get("performanceAttributes") or {}
            if performance.get("id"):
                slots[performance["id"]] = {
                    "start": attributes.get("appointmentWindowStart"),
                    "end": attributes.get("appointmentWindowEnd"),
                }
        if slots:
            state = store.get_state(call.txn, self.flow)
            store.update_state(call.txn, self.flow, slots={**state.slots, **slots})

    def build_init(self, request_obj, txn, state):
        request = booking_request(state, slot_id=request_obj.slot_id, patient=patient_info(request_obj))
        return AppointmentInitBuilder().message(request)

    def perform_init(self, request_obj: AppointmentInitSpec, txn, msg) -> None:
        if request_obj.message is None:
            store.update_state(txn, self.flow, init=request_obj.params())

    def build_confirm(self, request_obj, txn, state):
        request = booking_request(
            state,
            slot_id=request_obj.slot_id,
            patient=patient_info(request_obj),
            coordination_ref=request_obj.coordination_ref,
        )
        return AppointmentConfirmBuilder().message(request)

    def perform_confirm(self, request_obj: AppointmentConfirmSpec, txn, msg) -> None:
        if request_obj.message is None:
            store.update_state(txn, self.flow, confirm=request_obj.params())

    def build_status(self, request_obj, txn, state):
        request = BookingRequest(resource_id=state.resource_id or "", contract_id=contract_id_of(txn))
        return AppointmentStatusBuilder().message(request)

    def build_update(self, request_obj, txn, state):
        request = booking_request(state, slot_id=request_obj.slot_id, contract_id=contract_id_of(txn))
        return AppointmentUpdateBuilder().message(request)

    def build_cancel(self, request_obj, txn, state):
        request = BookingRequest(
            resource_id=state.resource_id or "",
            service_type=state.service_type or DEFAULT_SERVICE_TYPE,
            contract_id=contract_id_of(txn),
            cancellation_reason=request_obj.reason,
        )
        return AppointmentCancelBuilder().message(request)

    # ------------------------------------------------------------------ BPP
    def perform_on_select(self, request_obj, call):
        contract = request_obj.contract
        resource = resource_of(contract)
        slots = scheduling.list_slots_for_day(resource, requested_day(request_obj))
        service_type = contract.service_type or health_service_type(resource, scheduling.resource_schedules(resource))
        return echo_with_slots(call.message, slots, service_type)

    def perform_on_init(self, request_obj, call):
        contract = request_obj.contract
        resource = resource_of(contract)
        slot = slot_of(contract)
        if slot.end_datetime < timezone.now() or slot.allocated >= slot.availability.tokens_per_slot:
            raise BecknError(UNAVAILABLE, "the slot is no longer available")
        with suppress_beckn_signals():
            patient = find_or_create_appointment_patient(contract, resource.facility, scheduling.system_user())
        if patient is None:
            raise BecknError(INVALID_PAYLOAD, "a PATIENT participant is required")
        service_type = contract.service_type or service_type_of(call.message)
        return echo_with_slots(call.message, [slot], service_type)

    def perform_on_confirm(self, request_obj, call):
        contract = request_obj.contract
        resource = resource_of(contract)
        slot = slot_of(contract)
        user = scheduling.system_user()
        schedules = scheduling.resource_schedules(resource)
        service_type = contract.service_type or health_service_type(resource, schedules)
        with suppress_beckn_signals():
            patient = find_or_create_appointment_patient(contract, resource.facility, user)
            if patient is None:
                raise BecknError(INVALID_PAYLOAD, "a PATIENT participant is required")
            try:
                booking = scheduling.book(slot, patient, user, note=contract.coordination_ref)
            except DrfValidationError as exc:
                raise BecknError(UNAVAILABLE, _drf_text(exc)) from exc
            if acceptance_mode(schedules) == ACCEPTANCE_MODE_MANUAL_REVIEW:
                scheduling.hold_for_review(booking)
            else:
                scheduling.ensure_token(booking, user)
        attach(call, booking, patient, resource.facility, contract.coordination_ref)
        return echo_with_booking(call.message, booking, service_type)

    def perform_on_status(self, request_obj, call):
        booking = booking_of(call)
        service_type = service_type_of(call.message)
        return echo_with_booking(call.message, booking, service_type)

    def perform_on_cancel(self, request_obj, call):
        booking = booking_of(call)
        contract = request_obj.contract
        if booking.status in CANCELLED_BOOKING_STATUSES:
            raise BecknError(UNAVAILABLE, "the appointment is already cancelled")
        with suppress_beckn_signals():
            try:
                scheduling.cancel(booking, scheduling.system_user(), note=contract.cancellation_reason)
            except DrfValidationError as exc:
                raise BecknError(UNAVAILABLE, _drf_text(exc)) from exc
        booking.refresh_from_db()
        return echo_with_booking(call.message, booking, service_type_of(call.message))

    def perform_on_update(self, request_obj, call):
        contract = request_obj.contract
        booking = booking_of(call)
        new_slot = slot_of(contract)
        if new_slot.pk == booking.token_slot_id:
            raise BecknError(INVALID_PAYLOAD, "the appointment is already at that slot")
        user = scheduling.system_user()
        with suppress_beckn_signals():
            try:
                replacement = scheduling.reschedule(booking, new_slot, user, note=booking.note)
            except DrfValidationError as exc:
                raise BecknError(UNAVAILABLE, _drf_text(exc)) from exc
            scheduling.ensure_token(replacement, user)
        attach(call, replacement, booking.patient, new_slot.resource.facility)
        return echo_with_booking(call.message, replacement, service_type_of(call.message))
