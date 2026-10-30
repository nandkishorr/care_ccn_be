import datetime
from typing import Any

from django.utils import timezone
from pydantic import ValidationError as PydanticValidationError
from rest_framework.exceptions import ValidationError as DrfValidationError

from care_ccn_be import store
from care_ccn_be.bpp.mixins import (
    BppCancelMixin,
    BppConfirmMixin,
    BppDiscoverMixin,
    BppInitMixin,
    BppSelectMixin,
    BppStatusMixin,
    BppUpdateMixin,
)
from care_ccn_be.bpp.service import BppCall, BppServiceBase
from care_ccn_be.context_vars import suppress_beckn_signals
from care_ccn_be.exceptions import BecknError
from care_ccn_be.flows.appointment import patients
from care_ccn_be.flows.appointment.builders import (
    echo_with_booking,
    echo_with_slots,
    service_type_of,
)
from care_ccn_be.flows.appointment.catalog import acceptance_mode, health_service_type
from care_ccn_be.flows.appointment.constants import ACCEPTANCE_MODE_MANUAL_REVIEW, CANCELLED_BOOKING_STATUSES
from care_ccn_be.flows.appointment.models import AppointmentLink
from care_ccn_be.flows.appointment.schema import AppointmentContract
from care_ccn_be.protocol.builder import ContractBuilder
from care_ccn_be.services import scheduling

INVALID_PAYLOAD = "70002"
NOT_FOUND = "70004"
UNAVAILABLE = "70005"


class AppointmentContractBuilder(ContractBuilder):
    contract_model = AppointmentContract
    status_code = ""


def _drf_text(error: DrfValidationError) -> str:
    detail = error.detail
    if isinstance(detail, list | tuple) and detail:
        return str(detail[0])
    return str(detail)


class AppointmentBppService(
    BppDiscoverMixin,
    BppSelectMixin,
    BppInitMixin,
    BppConfirmMixin,
    BppCancelMixin,
    BppStatusMixin,
    BppUpdateMixin,
    BppServiceBase,
):
    contract_builder = AppointmentContractBuilder

    def contract(self, call: BppCall) -> AppointmentContract:
        try:
            return self.contract_builder.inbound(call.message)
        except PydanticValidationError as exc:
            raise BecknError(INVALID_PAYLOAD, f"invalid appointment contract: {exc.error_count()} error(s)") from exc

    @staticmethod
    def resource_of(contract: AppointmentContract):
        resource = scheduling.get_resource(contract.resource_id)
        if resource is None:
            raise BecknError(NOT_FOUND, f"unknown resource {contract.resource_id!r}")
        return resource

    @staticmethod
    def slot_of(contract: AppointmentContract, slot_id: str | None = None):
        slot = scheduling.get_slot(slot_id or contract.slot_id)
        if slot is None:
            raise BecknError(NOT_FOUND, f"unknown slot {slot_id or contract.slot_id!r}")
        if contract.resource_id and str(slot.resource.external_id) != contract.resource_id:
            raise BecknError(INVALID_PAYLOAD, "the slot does not belong to the selected resource")
        return slot

    @staticmethod
    def requested_day(call: BppCall, contract: AppointmentContract) -> datetime.date:
        intent = call.message.get("intent") or {}
        raw = intent.get("preferredDate") or intent.get("date")
        if raw:
            return datetime.date.fromisoformat(str(raw)[:10])
        start, _ = contract.slot_window
        if start is not None:
            return start.astimezone(datetime.UTC).date()
        return timezone.localdate()

    def active_link(self, call: BppCall) -> AppointmentLink | None:
        return (
            AppointmentLink.objects.select_related("token_booking", "token_booking__token_slot")
            .filter(transaction=call.txn, active=True)
            .first()
        )

    def booking_of(self, call: BppCall):
        link = self.active_link(call)
        if link is not None:
            return link.token_booking
        booking = scheduling.find_booking(((call.message.get("contract") or {}).get("id")) or call.txn.contract_id)
        if booking is None:
            raise BecknError(NOT_FOUND, "appointment not found")
        return booking

    def bpp_discover(self, call):
        return {"catalogs": self.flow.catalog_builder().catalogs()}

    def bpp_select(self, call):
        contract = self.contract(call)
        resource = self.resource_of(contract)
        try:
            day = self.requested_day(call, contract)
        except ValueError as exc:
            raise BecknError(INVALID_PAYLOAD, "invalid requested date") from exc
        slots = scheduling.list_slots_for_day(resource, day)
        service_type = contract.service_type or health_service_type(resource, scheduling.resource_schedules(resource))
        return echo_with_slots(call.message, slots, service_type)

    def bpp_init(self, call):
        contract = self.contract(call)
        resource = self.resource_of(contract)
        slot = self.slot_of(contract)
        if slot.end_datetime < timezone.now() or slot.allocated >= slot.availability.tokens_per_slot:
            raise BecknError(UNAVAILABLE, "the slot is no longer available")
        with suppress_beckn_signals():
            patient = patients.find_or_create_patient(contract, resource.facility, scheduling.system_user())
        if patient is None:
            raise BecknError(INVALID_PAYLOAD, "a PATIENT participant is required")
        service_type = contract.service_type or service_type_of(call.message)
        return echo_with_slots(call.message, [slot], service_type)

    def bpp_confirm(self, call):
        contract = self.contract(call)
        resource = self.resource_of(contract)
        slot = self.slot_of(contract)
        user = scheduling.system_user()
        schedules = scheduling.resource_schedules(resource)
        service_type = contract.service_type or health_service_type(resource, schedules)
        with suppress_beckn_signals():
            patient = patients.find_or_create_patient(contract, resource.facility, user)
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
        self.attach(call, booking, patient, resource.facility, contract.coordination_ref)
        return echo_with_booking(call.message, booking, service_type)

    def attach(self, call: BppCall, booking, patient, facility, coordination_ref: str = "") -> None:
        AppointmentLink.objects.filter(transaction=call.txn, active=True).update(active=False)
        AppointmentLink.objects.create(
            transaction=call.txn, token_booking=booking, beckn_ref=str(booking.external_id), active=True
        )
        fields: dict[str, Any] = {"contract_id": str(booking.external_id), "patient": patient, "facility": facility}
        if coordination_ref:
            fields["coordination_id"] = coordination_ref
        store.update_transaction(call.txn, **fields)

    def bpp_status(self, call):
        booking = self.booking_of(call)
        service_type = service_type_of(call.message)
        return echo_with_booking(call.message, booking, service_type)

    def bpp_cancel(self, call):
        booking = self.booking_of(call)
        contract = self.contract(call)
        if booking.status in CANCELLED_BOOKING_STATUSES:
            raise BecknError(UNAVAILABLE, "the appointment is already cancelled")
        with suppress_beckn_signals():
            try:
                scheduling.cancel(booking, scheduling.system_user(), note=contract.cancellation_reason)
            except DrfValidationError as exc:
                raise BecknError(UNAVAILABLE, _drf_text(exc)) from exc
        booking.refresh_from_db()
        return echo_with_booking(call.message, booking, service_type_of(call.message))

    def bpp_update(self, call):
        contract = self.contract(call)
        booking = self.booking_of(call)
        new_slot = self.slot_of(contract)
        if new_slot.pk == booking.token_slot_id:
            raise BecknError(INVALID_PAYLOAD, "the appointment is already at that slot")
        user = scheduling.system_user()
        with suppress_beckn_signals():
            try:
                replacement = scheduling.reschedule(booking, new_slot, user, note=booking.note)
            except DrfValidationError as exc:
                raise BecknError(UNAVAILABLE, _drf_text(exc)) from exc
            scheduling.ensure_token(replacement, user)
        self.attach(call, replacement, booking.patient, new_slot.resource.facility)
        return echo_with_booking(call.message, replacement, service_type_of(call.message))
