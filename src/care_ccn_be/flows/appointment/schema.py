from datetime import datetime
from typing import Any

from pydantic import model_validator

from care_ccn_be.protocol.schema import BecknRequest, Contract, ContractMessage, Participant
from care_ccn_be.protocol.schema.health import HealthContract, HealthPerformance

PATIENT = "PATIENT"


class AppointmentContract(Contract):
    """Typed accessors over the appointment contract (HealthContract attributes, PATIENT participant, slot)."""

    @property
    def patient(self) -> Participant | None:
        return self.participant(PATIENT)

    @property
    def resource_id(self) -> str | None:
        return next((r.id for c in self.commitments for r in c.resources), None)

    @property
    def offer_id(self) -> str | None:
        return next((c.offer.id for c in self.commitments if c.offer), None)

    @property
    def slot_id(self) -> str | None:
        return next((p.id for p in self.performance if p.id), None)

    @property
    def slot_window(self) -> tuple[datetime | None, datetime | None]:
        for performance in self.performance:
            attributes = performance.performance_attributes
            if isinstance(attributes, HealthPerformance):
                return attributes.appointment_window_start, attributes.appointment_window_end
        return None, None

    @property
    def service_type(self) -> str | None:
        return getattr(self.contract_attributes, "health_service_type", None)

    def attribute_extra(self, key: str) -> Any:
        return (getattr(self.contract_attributes, "model_extra", None) or {}).get(key)

    @property
    def coordination_ref(self) -> str:
        return str(self.attribute_extra("coordinationRef") or "")

    @property
    def cancellation_reason(self) -> str | None:
        return self.attribute_extra("cancellationReason")

    @model_validator(mode="after")
    def _health_contract(self):
        if self.contract_attributes is not None and not isinstance(self.contract_attributes, HealthContract):
            raise ValueError("contractAttributes must be hct:HealthContract for the appointment flow")
        return self


class AppointmentMessage(ContractMessage[AppointmentContract]):
    pass


class AppointmentInitMessage(AppointmentMessage):
    @model_validator(mode="after")
    def _booking_inputs(self):
        contract = self.contract
        required = {
            "commitments[].resources[].id": contract.resource_id,
            "performance[].id (slot)": contract.slot_id,
            "participants[PATIENT]": contract.patient,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ValueError("missing " + ", ".join(missing))
        return self


class AppointmentConfirmMessage(AppointmentInitMessage):
    pass


AppointmentRequest = BecknRequest[AppointmentMessage]
AppointmentInitRequest = BecknRequest[AppointmentInitMessage]
AppointmentConfirmRequest = BecknRequest[AppointmentConfirmMessage]
