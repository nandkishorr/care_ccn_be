import copy
import json
import pathlib
from datetime import UTC, date, datetime
from types import SimpleNamespace

from django.test import SimpleTestCase
from pydantic import ValidationError

from care_ccn_be.flows.appointment.builders import (
    AppointmentCancelBuilder,
    AppointmentConfirmBuilder,
    AppointmentInitBuilder,
    AppointmentSelectBuilder,
    AppointmentStatusBuilder,
    AppointmentUpdateBuilder,
    BookingRequest,
    PatientInfo,
    booking_contract_status,
    echo_with_booking,
    echo_with_slots,
    service_type_of,
)
from care_ccn_be.flows.appointment.schema import (
    AppointmentConfirmRequest,
    AppointmentContract,
    AppointmentInitRequest,
    AppointmentRequest,
)
from care_ccn_be.protocol.builder import Part
from care_ccn_be.protocol.schema import Consideration, HasConsideration, JsonLdModel, Price
from care_ccn_be.protocol.schema.health import HealthContract, HealthParticipant, HealthPerformance

PAYLOADS = pathlib.Path(__file__).parent / "payloads"
INIT = json.loads((PAYLOADS / "init.json").read_text())
CONFIRM = json.loads((PAYLOADS / "confirm.json").read_text())
RESOURCE = "7e772edf-4c5f-4707-aeee-3fa6830aa1c2"
SLOT = "19a946fb-c4a6-40e1-88e7-6416feaa9626"


def booking_request(**overrides) -> BookingRequest:
    values = {
        "resource_id": RESOURCE,
        "slot_id": SLOT,
        "window_start": datetime(2026, 6, 19, 16, 6, tzinfo=UTC),
        "window_end": datetime(2026, 6, 19, 16, 30, tzinfo=UTC),
        "service_type": "PHYSICAL_CONSULTATION",
        "patient": PatientInfo(
            name="Meena Joshi", gender="FEMALE", dob=date(1985, 4, 12), language="en", abha="91-1234-5678-9012"
        ),
    }
    return BookingRequest(**{**values, **overrides})


class SchemaTests(SimpleTestCase):
    def test_typed_access(self):
        contract = AppointmentInitRequest.model_validate(INIT).message.contract
        attrs = contract.patient.participant_attributes
        self.assertEqual(attrs.health_ids[0].value, "91-1234-5678-9012")
        self.assertEqual(attrs.date_of_birth, date(1985, 4, 12))
        self.assertIsInstance(contract.contract_attributes, HealthContract)
        self.assertIsInstance(contract.performance[0].performance_attributes, HealthPerformance)
        self.assertIsInstance(attrs, HealthParticipant)
        self.assertEqual(contract.resource_id, RESOURCE)
        self.assertEqual(contract.slot_id, SLOT)
        self.assertEqual(contract.offer_id, f"offer-{RESOURCE}")
        self.assertEqual(contract.service_type, "PHYSICAL_CONSULTATION")
        self.assertEqual(contract.slot_window[0], datetime(2026, 6, 19, 16, 6, tzinfo=UTC))

    def test_lossless_round_trip(self):
        self.assertEqual(AppointmentInitRequest.model_validate(INIT).dump(), INIT)
        self.assertEqual(AppointmentConfirmRequest.model_validate(CONFIRM).dump(), CONFIRM)

    def test_unknown_fields_and_types_survive(self):
        future = copy.deepcopy(CONFIRM)
        contract = future["message"]["contract"]
        contract["consideration"] = [{"id": "c1", "price": {"currency": "INR", "value": "500"}}]
        contract["participants"][0]["participantAttributes"]["preferredContact"] = "SMS"
        contract["commitments"][0]["commitmentAttributes"] = {
            "@context": "https://schema.beckn.io/HealthBilling/v2.1/context.jsonld",
            "@type": "hbl:HealthBilling",
            "payer": "SELF",
        }
        parsed = AppointmentConfirmRequest.model_validate(future)
        self.assertEqual(parsed.dump(), future)
        self.assertIs(type(parsed.message.contract.commitments[0].commitment_attributes), JsonLdModel)

    def test_init_requires_resource_slot_and_patient(self):
        for removed in ("commitments", "performance", "participants"):
            bad = copy.deepcopy(INIT)
            del bad["message"]["contract"][removed]
            with self.assertRaises(ValidationError, msg=removed):
                AppointmentInitRequest.model_validate(bad)
        AppointmentRequest.model_validate(bad)

    def test_contract_attributes_must_be_a_health_contract(self):
        bad = copy.deepcopy(INIT)
        bad["message"]["contract"]["contractAttributes"] = {"@type": "hbl:Other"}
        with self.assertRaises(ValidationError):
            AppointmentInitRequest.model_validate(bad)

    def test_extras_of_the_contract_attributes(self):
        payload = copy.deepcopy(INIT)
        payload["message"]["contract"]["contractAttributes"].update(
            {"coordinationRef": "ref-1", "cancellationReason": "unwell"}
        )
        contract = AppointmentInitRequest.model_validate(payload).message.contract
        self.assertEqual(contract.coordination_ref, "ref-1")
        self.assertEqual(contract.cancellation_reason, "unwell")
        plain = AppointmentInitRequest.model_validate(INIT).message.contract
        self.assertEqual((plain.coordination_ref, plain.cancellation_reason), ("", None))

    def test_naive_timestamp_rejected(self):
        bad = copy.deepcopy(INIT)
        bad["context"]["timestamp"] = "2026-06-19T16:06:00"
        with self.assertRaises(ValidationError):
            AppointmentInitRequest.model_validate(bad)


class BuilderTests(SimpleTestCase):
    def test_init_reproduces_the_sample_contract(self):
        self.assertEqual(AppointmentInitBuilder().build(booking_request()).dump(), INIT["message"]["contract"])

    def test_confirm_reproduces_the_sample_contract(self):
        request = booking_request(
            slot_id="d18aa09d-1b7d-4695-912d-53099d49b8a3",
            window_start=datetime(2026, 6, 30, 16, 6, tzinfo=UTC),
            window_end=datetime(2026, 6, 30, 16, 30, tzinfo=UTC),
        )
        self.assertEqual(AppointmentConfirmBuilder().build(request).dump(), CONFIRM["message"]["contract"])

    def test_appended_part_is_emitted_and_rest_unchanged(self):
        class ConsiderationPart(Part):
            field = "consideration"

            def build(self, spec, builder):
                return [Consideration(id="c1", price=Price(currency="INR", value="500"))]

        class Paid(AppointmentContract, HasConsideration):
            pass

        class PaidBuilder(AppointmentInitBuilder):
            contract_model = Paid
            parts = (*AppointmentInitBuilder.parts, ConsiderationPart)

        paid = PaidBuilder().build(booking_request()).dump()
        self.assertEqual(paid["consideration"], [{"id": "c1", "price": {"currency": "INR", "value": "500"}}])
        self.assertEqual({k: v for k, v in paid.items() if k != "consideration"}, INIT["message"]["contract"])

    def test_parts_returning_none_are_skipped(self):
        contract = AppointmentInitBuilder().build(booking_request(patient=None, slot_id=None)).dump()
        self.assertNotIn("participants", contract)
        self.assertNotIn("performance", contract)
        self.assertEqual(contract["status"], {"code": "ACTIVE"})

    def test_select_is_a_draft_with_the_requested_day_as_an_untied_window(self):
        contract = AppointmentSelectBuilder().build(booking_request(slot_id=None, patient=None)).dump()
        self.assertEqual(contract["status"], {"code": "DRAFT"})
        (performance,) = contract["performance"]
        self.assertNotIn("id", performance)
        self.assertEqual(performance["performanceAttributes"]["appointmentWindowStart"], "2026-06-19T16:06:00+00:00")
        self.assertNotIn("participants", contract)

    def test_select_without_a_day_has_no_performance(self):
        contract = AppointmentSelectBuilder().build(booking_request(window_start=None, window_end=None)).dump()
        self.assertNotIn("performance", contract)

    def test_update_carries_the_contract_id(self):
        contract = AppointmentUpdateBuilder().build(booking_request(contract_id="booking-1")).dump()
        self.assertEqual(contract["id"], "booking-1")
        self.assertEqual(contract["performance"][0]["id"], SLOT)

    def test_cancel_carries_id_status_and_reason(self):
        contract = (
            AppointmentCancelBuilder()
            .build(booking_request(contract_id="booking-1", cancellation_reason="unwell"))
            .dump()
        )
        self.assertEqual(contract["id"], "booking-1")
        self.assertEqual(contract["status"], {"code": "CANCELLED"})
        self.assertEqual(contract["contractAttributes"]["cancellationReason"], "unwell")

    def test_status_is_only_the_contract_id(self):
        contract = AppointmentStatusBuilder().build(booking_request(contract_id="booking-1")).dump()
        self.assertEqual(contract, {"id": "booking-1"})

    def test_coordination_ref_is_a_contract_attribute(self):
        contract = AppointmentConfirmBuilder().build(booking_request(coordination_ref="ref-1")).dump()
        self.assertEqual(contract["contractAttributes"]["coordinationRef"], "ref-1")

    def test_echo_sets_status_and_keeps_unknown_fields(self):
        inbound = copy.deepcopy(INIT["message"])
        inbound["contract"]["somethingNew"] = {"a": 1}
        echoed = AppointmentInitBuilder.echo(inbound, "DRAFT")
        self.assertEqual(echoed["contract"]["status"]["code"], "DRAFT")
        self.assertEqual(echoed["contract"]["somethingNew"], {"a": 1})
        self.assertEqual(inbound["contract"]["status"]["code"], "ACTIVE")

    def test_inbound_parses_contract(self):
        self.assertEqual(AppointmentInitBuilder.inbound(INIT["message"]).resource_id, RESOURCE)


class BppRenderingTests(SimpleTestCase):
    def slot(self):
        return SimpleNamespace(
            external_id="slot-1",
            start_datetime=datetime(2026, 6, 19, 9, 0, tzinfo=UTC),
            end_datetime=datetime(2026, 6, 19, 9, 30, tzinfo=UTC),
        )

    def booking(self, status):
        return SimpleNamespace(external_id="booking-1", status=status, token_slot=self.slot())

    def test_booking_status_maps_to_the_contract_status(self):
        mapping = {
            "booked": "ACTIVE",
            "arrived": "ACTIVE",
            "noshow": "ACTIVE",
            "pending": "DRAFT",
            "proposed": "DRAFT",
            "waitlist": "DRAFT",
            "cancelled": "CANCELLED",
            "entered_in_error": "CANCELLED",
            "rescheduled": "CANCELLED",
            "fulfilled": "COMPLETED",
            "checked_in": "COMPLETED",
            "in_consultation": "COMPLETED",
        }
        for status, expected in mapping.items():
            self.assertEqual(booking_contract_status(self.booking(status)), expected, status)

    def test_echo_with_slots_lists_every_slot_as_a_performance(self):
        message = echo_with_slots({"contract": {"id": "keep"}}, [self.slot()], "LAB_TEST")
        contract = message["contract"]
        self.assertEqual(contract["id"], "keep")
        self.assertEqual(contract["status"]["code"], "DRAFT")
        (performance,) = contract["performance"]
        self.assertEqual(performance["id"], "slot-1")
        self.assertEqual(performance["performanceAttributes"]["healthServiceType"], "LAB_TEST")
        self.assertNotIn("confirmedAppointmentTime", performance["performanceAttributes"])

    def test_echo_with_booking_stamps_the_confirmed_time(self):
        inbound = {"contract": {"id": "old", "somethingNew": 1}}
        message = echo_with_booking(inbound, self.booking("booked"), "PHYSICAL_CONSULTATION")
        contract = message["contract"]
        self.assertEqual((contract["id"], contract["status"]["code"]), ("booking-1", "ACTIVE"))
        self.assertEqual(contract["somethingNew"], 1)
        attributes = contract["performance"][0]["performanceAttributes"]
        self.assertEqual(attributes["confirmedAppointmentTime"], "2026-06-19T09:00:00+00:00")
        self.assertEqual(inbound["contract"]["id"], "old")

    def test_echo_with_booking_status_override(self):
        message = echo_with_booking({"contract": {}}, self.booking("booked"), "LAB_TEST", status="DRAFT")
        self.assertEqual(message["contract"]["status"]["code"], "DRAFT")

    def test_service_type_of_falls_back_to_the_default(self):
        self.assertEqual(service_type_of({}), "PHYSICAL_CONSULTATION")
        self.assertEqual(
            service_type_of({"contract": {"contractAttributes": {"healthServiceType": "LAB_TEST"}}}), "LAB_TEST"
        )
