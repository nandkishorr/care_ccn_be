from datetime import date, datetime
from typing import Annotated, Any

from pydantic import BeforeValidator, Field, model_validator

from care_ccn_be.resources.beckn import BecknModel, BecknRequest, Contract, ContractMessage, Participant
from care_ccn_be.resources.beckn.codes import ParticipantRole
from care_ccn_be.resources.beckn.health import HealthContract, HealthPerformance

PATIENT = ParticipantRole.PATIENT


def _day_part(value: Any) -> Any:
    return value[:10] if isinstance(value, str) else value


RequestedDay = Annotated[date, BeforeValidator(_day_part)]


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
    def health_contract(self) -> HealthContract:
        attributes = self.contract_attributes
        return attributes if isinstance(attributes, HealthContract) else HealthContract()

    @property
    def service_type(self) -> str | None:
        return self.health_contract.health_service_type

    @property
    def coordination_ref(self) -> str:
        return self.health_contract.coordination_ref or ""

    @property
    def cancellation_reason(self) -> str | None:
        return self.health_contract.cancellation_reason

    @model_validator(mode="after")
    def _health_contract(self):
        if self.contract_attributes is not None and not isinstance(self.contract_attributes, HealthContract):
            raise ValueError("contractAttributes must be hct:HealthContract for the appointment flow")
        return self


class AppointmentMessage(ContractMessage[AppointmentContract]):
    pass


class SelectIntent(BecknModel):
    """Care extension on ``select``: the day whose slots are wanted (``preferredDate``, or ``date``)."""

    preferred_date: RequestedDay | None = None
    on_date: RequestedDay | None = Field(default=None, alias="date")

    @property
    def day(self) -> date | None:
        return self.preferred_date or self.on_date


class AppointmentSelectMessage(AppointmentMessage):
    intent: SelectIntent | None = None


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
AppointmentSelectRequest = BecknRequest[AppointmentSelectMessage]
AppointmentInitRequest = BecknRequest[AppointmentInitMessage]
AppointmentConfirmRequest = BecknRequest[AppointmentConfirmMessage]
