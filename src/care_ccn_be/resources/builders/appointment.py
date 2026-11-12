"""Outbound appointment contracts (BAP side, composed of parts) and the BPP-side contract rendering of a booking."""

import copy
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, ClassVar

from care_ccn_be.constants import (
    CANCELLED_BOOKING_STATUSES,
    COMPLETED_BOOKING_STATUSES,
    DEFAULT_SERVICE_TYPE,
    PENDING_BOOKING_STATUSES,
)
from care_ccn_be.protocol.actions import CONTRACT_ACTIVE, CONTRACT_CANCELLED, CONTRACT_COMPLETED, CONTRACT_DRAFT
from care_ccn_be.protocol.builder import ContractBuilder, Part
from care_ccn_be.resources.appointment.schema import PATIENT, AppointmentContract
from care_ccn_be.resources.beckn import Commitment, Descriptor, Offer, Participant, Performance, Quantity, Resource
from care_ccn_be.resources.beckn import Status as StatusModel
from care_ccn_be.resources.beckn.health import HealthContract, HealthId, HealthParticipant, HealthPerformance

ABHA_SYSTEM = "ABHA"


class AppointmentContractBuilder(ContractBuilder):
    contract_model = AppointmentContract
    status_code = ""


@dataclass
class PatientInfo:
    name: str
    gender: str | None = None
    dob: date | None = None
    language: str | None = None
    abha: str | None = None


@dataclass
class BookingRequest:
    """What every outbound appointment builder reads; assembled from the FE spec, the stored state and the patient."""

    resource_id: str
    slot_id: str | None = None
    offer_id: str | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None
    service_type: str = DEFAULT_SERVICE_TYPE
    patient: PatientInfo | None = None
    coordination_ref: str | None = None
    cancellation_reason: str | None = None
    contract_id: str | None = None


class ContractIdPart(Part):
    field = "id"

    def build(self, spec, builder):
        return spec.contract_id or None


class StatusPart(Part):
    field = "status"

    def build(self, spec, builder):
        return StatusModel(code=builder.status_code)


class CommitmentPart(Part):
    field = "commitments"

    def build(self, spec, builder):
        rid = spec.resource_id
        if not rid:
            return None
        return [
            Commitment(
                id=f"commitment-{rid}",
                status=StatusModel(descriptor=Descriptor(code=builder.status_code)),
                resources=[Resource(id=rid, quantity=Quantity(count=1))],
                offer=Offer(id=spec.offer_id or f"offer-{rid}", resource_ids=[rid]),
            )
        ]


class PatientParticipantPart(Part):
    field = "participants"

    def build(self, spec, builder):
        patient = spec.patient
        if patient is None:
            return None
        return [
            Participant(
                descriptor=Descriptor(name=patient.name, short_desc="Patient (Subject)"),
                participant_attributes=HealthParticipant(
                    participant_role=PATIENT,
                    primary_language=patient.language or None,
                    gender=patient.gender or None,
                    date_of_birth=patient.dob,
                    health_ids=[HealthId(system=ABHA_SYSTEM, value=patient.abha)] if patient.abha else None,
                ),
            )
        ]


class SlotPerformancePart(Part):
    field = "performance"

    def build(self, spec, builder):
        if not spec.slot_id:
            return None
        return [
            Performance(
                id=spec.slot_id,
                performance_attributes=HealthPerformance(
                    health_service_type=spec.service_type,
                    appointment_window_start=spec.window_start,
                    appointment_window_end=spec.window_end,
                ),
            )
        ]


class RequestedDayPart(Part):
    """Select carries the requested day as an (untied) performance window."""

    field = "performance"

    def build(self, spec, builder):
        if spec.window_start is None:
            return None
        return [
            Performance(
                performance_attributes=HealthPerformance(
                    health_service_type=spec.service_type,
                    appointment_window_start=spec.window_start,
                    appointment_window_end=spec.window_end,
                )
            )
        ]


class HealthContractPart(Part):
    field = "contract_attributes"

    def build(self, spec, builder):
        return HealthContract(
            health_service_type=spec.service_type,
            coordination_ref=spec.coordination_ref or None,
            cancellation_reason=spec.cancellation_reason or None,
        )


class AppointmentSelectBuilder(ContractBuilder):
    contract_model = AppointmentContract
    status_code: ClassVar[str] = CONTRACT_DRAFT
    parts = (StatusPart, CommitmentPart, RequestedDayPart, HealthContractPart)


class AppointmentInitBuilder(ContractBuilder):
    contract_model = AppointmentContract
    parts = (StatusPart, CommitmentPart, PatientParticipantPart, SlotPerformancePart, HealthContractPart)


class AppointmentConfirmBuilder(AppointmentInitBuilder):
    pass


class AppointmentUpdateBuilder(AppointmentInitBuilder):
    """Reschedule: the booked contract, pointing at the new slot."""

    parts = (ContractIdPart, *AppointmentInitBuilder.parts)


class AppointmentCancelBuilder(ContractBuilder):
    contract_model = AppointmentContract
    status_code: ClassVar[str] = CONTRACT_CANCELLED
    parts = (ContractIdPart, StatusPart, HealthContractPart)


class AppointmentStatusBuilder(ContractBuilder):
    contract_model = AppointmentContract
    status_code: ClassVar[str] = ""
    parts = (ContractIdPart,)


def booking_contract_status(booking) -> str:
    if booking.status in CANCELLED_BOOKING_STATUSES:
        return CONTRACT_CANCELLED
    if booking.status in COMPLETED_BOOKING_STATUSES:
        return CONTRACT_COMPLETED
    if booking.status in PENDING_BOOKING_STATUSES:
        return CONTRACT_DRAFT
    return CONTRACT_ACTIVE


def slot_performance(slot, service_type: str, confirmed: bool = False) -> dict[str, Any]:
    """A ``TokenSlot`` as a performance entry; ``confirmed`` also stamps the confirmed appointment time."""
    return Performance(
        id=str(slot.external_id),
        performance_attributes=HealthPerformance(
            health_service_type=service_type,
            appointment_window_start=slot.start_datetime,
            appointment_window_end=slot.end_datetime,
            confirmed_appointment_time=slot.start_datetime if confirmed else None,
        ),
    ).dump()


def service_type_of(message: dict[str, Any], default: str = DEFAULT_SERVICE_TYPE) -> str:
    attributes = ((message or {}).get("contract") or {}).get("contractAttributes") or {}
    return attributes.get("healthServiceType") or default


def echo_with_slots(message: dict[str, Any], slots, service_type: str) -> dict[str, Any]:
    body = ContractBuilder.echo(copy.deepcopy(message), CONTRACT_DRAFT)
    body["contract"]["performance"] = [slot_performance(slot, service_type) for slot in slots]
    return body


def echo_with_booking(message: dict[str, Any], booking, service_type: str, status: str | None = None) -> dict[str, Any]:
    """Echo the inbound contract with the Care booking as its id and the booked slot as its only performance."""
    body = ContractBuilder.echo(copy.deepcopy(message), status or booking_contract_status(booking))
    body["contract"]["id"] = str(booking.external_id)
    body["contract"]["performance"] = [slot_performance(booking.token_slot, service_type, confirmed=True)]
    return body
