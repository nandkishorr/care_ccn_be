"""Request specs of the appointment BAP APIs, the flow state and the appointment transaction retrieve spec."""

import datetime as dt
from typing import Any

from pydantic import UUID4

from care_ccn_be.flows.base import FlowState
from care_ccn_be.resources.appointment.schema import (
    AppointmentConfirmMessage,
    AppointmentInitMessage,
    AppointmentMessage,
)
from care_ccn_be.resources.bap.spec import BapActionSpec
from care_ccn_be.resources.transaction.spec import BecknTransactionRetrieveSpec


class AppointmentState(FlowState):
    resource_id: str | None = None
    offer_id: str | None = None
    service_type: str | None = None
    slots: dict[str, dict] = {}
    init: dict | None = None
    confirm: dict | None = None


class AppointmentSelectSpec(BapActionSpec):
    __required__ = ("resource_id",)

    message: AppointmentMessage | None = None
    resource_id: str | None = None
    offer_id: str | None = None
    date: dt.date | None = None
    health_service_type: str | None = None


class AppointmentInitSpec(BapActionSpec):
    __required__ = ("slot_id", "patient")

    message: AppointmentInitMessage | None = None
    slot_id: str | None = None
    patient: UUID4 | None = None


class AppointmentConfirmSpec(AppointmentInitSpec):
    message: AppointmentConfirmMessage | None = None
    coordination_ref: str | None = None


class AppointmentRescheduleSpec(BapActionSpec):
    __required__ = ("slot_id",)

    message: AppointmentMessage | None = None
    slot_id: str | None = None


class AppointmentCancelSpec(BapActionSpec):
    message: AppointmentMessage | None = None
    reason: str | None = None


class AppointmentStatusSpec(BapActionSpec):
    message: AppointmentMessage | None = None


class AppointmentTransactionRetrieveSpec(BecknTransactionRetrieveSpec):
    confirm: dict[str, Any] | None = None

    @classmethod
    def perform_extra_serialization(cls, mapping, obj, *args, **kwargs):
        super().perform_extra_serialization(mapping, obj, *args, **kwargs)
        mapping["confirm"] = (obj.state or {}).get("confirm")
