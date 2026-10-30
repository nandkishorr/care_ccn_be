"""Typed request bodies of the appointment BAP endpoints (camelCase on the wire, snake_case accepted)."""

import datetime as dt

from pydantic import model_validator

from care_ccn_be.bap.specs import BapSpec
from care_ccn_be.flows.base import FlowState

RESOURCE_REQUIRED = "resourceId is required to select an appointment resource"
SLOT_REQUIRED = "slotId is required"
PATIENT_REQUIRED = "patient is required"


class AppointmentState(FlowState):
    resource_id: str | None = None
    offer_id: str | None = None
    service_type: str | None = None
    slots: dict[str, dict] = {}
    init: dict | None = None
    confirm: dict | None = None


class AppointmentSelectSpec(BapSpec):
    offer_id: str | None = None
    resource_id: str | None = None
    date: dt.date | None = None
    health_service_type: str | None = None

    @model_validator(mode="after")
    def _resource(self):
        if self.message is None and not self.resource_id:
            raise ValueError(RESOURCE_REQUIRED)
        return self


class AppointmentInitSpec(BapSpec):
    slot_id: str | None = None
    patient: str | None = None

    @model_validator(mode="after")
    def _booking(self):
        if self.message is None:
            if not self.slot_id:
                raise ValueError(SLOT_REQUIRED)
            if not self.patient:
                raise ValueError(PATIENT_REQUIRED)
        return self

    def params(self) -> dict:
        return {"slotId": self.slot_id, "patient": self.patient}


class AppointmentConfirmSpec(AppointmentInitSpec):
    coordination_ref: str | None = None

    def params(self) -> dict:
        params = super().params()
        if self.coordination_ref:
            params["coordinationRef"] = self.coordination_ref
        return params


class AppointmentCancelSpec(BapSpec):
    reason: str | None = None


class AppointmentRescheduleSpec(BapSpec):
    slot_id: str | None = None

    @model_validator(mode="after")
    def _slot(self):
        if self.message is None and not self.slot_id:
            raise ValueError(SLOT_REQUIRED)
        return self
