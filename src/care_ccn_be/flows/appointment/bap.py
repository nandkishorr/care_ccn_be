from datetime import datetime
from typing import Any

from care_ccn_be import store
from care_ccn_be.bap.mixins import (
    BapCancelMixin,
    BapConfirmMixin,
    BapDiscoverMixin,
    BapInitMixin,
    BapSelectMixin,
    BapStatusMixin,
    BapUpdateMixin,
)
from care_ccn_be.bap.specs import BapSource, ContractRefSpec
from care_ccn_be.bap.viewset import BapViewSetBase
from care_ccn_be.exceptions import BecknError
from care_ccn_be.flows.appointment import patients
from care_ccn_be.flows.appointment.builders import (
    AppointmentCancelBuilder,
    AppointmentConfirmBuilder,
    AppointmentInitBuilder,
    AppointmentSelectBuilder,
    AppointmentStatusBuilder,
    AppointmentUpdateBuilder,
    BookingRequest,
)
from care_ccn_be.flows.appointment.constants import DEFAULT_SERVICE_TYPE
from care_ccn_be.flows.appointment.flow import AppointmentFlow
from care_ccn_be.flows.appointment.schema import (
    AppointmentConfirmMessage,
    AppointmentInitMessage,
    AppointmentMessage,
)
from care_ccn_be.flows.appointment.specs import (
    AppointmentCancelSpec,
    AppointmentConfirmSpec,
    AppointmentInitSpec,
    AppointmentRescheduleSpec,
    AppointmentSelectSpec,
)
from care_ccn_be.protocol.actions import CANCEL, CONFIRM, INIT, SELECT, STATUS, UPDATE

INVALID_PAYLOAD = "70002"
PASSTHROUGH_MODELS = {
    SELECT: AppointmentMessage,
    INIT: AppointmentInitMessage,
    CONFIRM: AppointmentConfirmMessage,
    STATUS: AppointmentMessage,
    UPDATE: AppointmentMessage,
    CANCEL: AppointmentMessage,
}


def day_window(day) -> tuple[datetime, datetime]:
    from datetime import UTC, time

    return datetime.combine(day, time.min, tzinfo=UTC), datetime.combine(day, time(23, 59, 59), tzinfo=UTC)


def catalog_service_type(txn, resource_id: str) -> str | None:
    for catalog in store.merged_catalogs(txn):
        for resource in catalog.get("resources") or []:
            if resource.get("id") == resource_id:
                return (resource.get("resourceAttributes") or {}).get("healthServiceType")
    return None


def parse_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class AppointmentBapViewSet(
    BapDiscoverMixin,
    BapSelectMixin,
    BapInitMixin,
    BapConfirmMixin,
    BapStatusMixin,
    BapUpdateMixin,
    BapCancelMixin,
    BapViewSetBase,
):
    flow = AppointmentFlow
    spec_models = {
        **BapViewSetBase.spec_models,
        SELECT: AppointmentSelectSpec,
        INIT: AppointmentInitSpec,
        CONFIRM: AppointmentConfirmSpec,
        STATUS: ContractRefSpec,
        UPDATE: AppointmentRescheduleSpec,
        CANCEL: AppointmentCancelSpec,
    }

    def validate_passthrough(self, action: str, message: dict[str, Any]) -> dict[str, Any]:
        model = PASSTHROUGH_MODELS.get(action)
        return model.model_validate(message).dump() if model else message

    @staticmethod
    def contract_id(source: BapSource) -> str:
        if not source.txn.contract_id:
            raise BecknError(INVALID_PAYLOAD, "the appointment is not confirmed yet")
        return source.txn.contract_id

    @staticmethod
    def booking_request(source: BapSource, **values) -> BookingRequest:
        state = source.state
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

    def bap_select(self, source: BapSource):
        spec = source.spec
        service_type = (
            spec.health_service_type
            or catalog_service_type(source.txn, spec.resource_id)
            or source.state.service_type
            or DEFAULT_SERVICE_TYPE
        )
        window = day_window(spec.date) if spec.date else (None, None)
        state = store.update_state(
            source.txn,
            self.flow,
            resource_id=spec.resource_id,
            offer_id=spec.offer_id or f"offer-{spec.resource_id}",
            service_type=service_type,
        )
        request = BookingRequest(
            resource_id=spec.resource_id,
            offer_id=state.offer_id,
            service_type=service_type,
            window_start=window[0],
            window_end=window[1],
        )
        return AppointmentSelectBuilder().message(request)

    def bap_init(self, source: BapSource):
        request = self.booking_request(
            source,
            slot_id=source.spec.slot_id,
            patient=patients.patient_info(source.patient),
        )
        return AppointmentInitBuilder().message(request)

    def bap_confirm(self, source: BapSource):
        request = self.booking_request(
            source,
            slot_id=source.spec.slot_id,
            patient=patients.patient_info(source.patient),
            coordination_ref=source.spec.coordination_ref,
        )
        return AppointmentConfirmBuilder().message(request)

    def bap_status(self, source: BapSource):
        return AppointmentStatusBuilder().message(
            BookingRequest(resource_id=source.state.resource_id or "", contract_id=self.contract_id(source))
        )

    def bap_update(self, source: BapSource):
        request = self.booking_request(source, slot_id=source.spec.slot_id, contract_id=self.contract_id(source))
        return AppointmentUpdateBuilder().message(request)

    def bap_cancel(self, source: BapSource):
        request = BookingRequest(
            resource_id=source.state.resource_id or "",
            service_type=source.state.service_type or DEFAULT_SERVICE_TYPE,
            contract_id=self.contract_id(source),
            cancellation_reason=source.spec.reason,
        )
        return AppointmentCancelBuilder().message(request)

    def after_send(self, action: str, source: BapSource, msg) -> None:
        if action in (INIT, CONFIRM) and source.spec.message is None:
            store.update_state(source.txn, self.flow, **{action: source.spec.params()})
