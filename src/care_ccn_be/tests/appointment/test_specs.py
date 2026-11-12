import datetime

from django.test import SimpleTestCase
from pydantic import ValidationError

from care_ccn_be.resources.appointment.schema import AppointmentMessage
from care_ccn_be.resources.appointment.spec import (
    AppointmentCancelSpec,
    AppointmentConfirmSpec,
    AppointmentInitSpec,
    AppointmentRescheduleSpec,
    AppointmentSelectSpec,
)

PATIENT = "6a1d2c3b-4e5f-4a6b-8c7d-9e0f1a2b3c4d"


class SpecTests(SimpleTestCase):
    def test_select_takes_snake_case_fields(self):
        spec = AppointmentSelectSpec.model_validate({"offer_id": "o", "resource_id": "r", "date": "2026-07-01"})
        self.assertEqual((spec.resource_id, spec.offer_id, spec.date), ("r", "o", datetime.date(2026, 7, 1)))
        with self.assertRaises(ValidationError):
            AppointmentSelectSpec.model_validate({"offerId": "o", "resourceId": "r"})

    def test_select_requires_a_resource_unless_a_message_is_passed_through(self):
        with self.assertRaisesMessage(ValidationError, "resource_id is required"):
            AppointmentSelectSpec.model_validate({"date": "2026-07-01"})
        self.assertIsNone(AppointmentSelectSpec.model_validate({"message": {"contract": {}}}).resource_id)

    def test_select_rejects_a_bad_date(self):
        with self.assertRaises(ValidationError):
            AppointmentSelectSpec.model_validate({"resource_id": "r", "date": "tomorrow"})

    def test_passthrough_messages_use_the_action_message_schema(self):
        self.assertIsInstance(
            AppointmentSelectSpec.model_validate({"message": {"contract": {}}}).message,
            AppointmentMessage,
        )
        with self.assertRaises(ValidationError):
            AppointmentInitSpec.model_validate({"message": {"contract": {}}})

    def test_init_requires_slot_and_patient(self):
        with self.assertRaisesMessage(ValidationError, "slot_id, patient are required"):
            AppointmentInitSpec.model_validate({})
        with self.assertRaisesMessage(ValidationError, "slot_id is required"):
            AppointmentInitSpec.model_validate({"patient": PATIENT})
        with self.assertRaisesMessage(ValidationError, "patient is required"):
            AppointmentInitSpec.model_validate({"slot_id": "s"})
        with self.assertRaises(ValidationError):
            AppointmentInitSpec.model_validate({"slot_id": "s", "patient": "not-a-uuid"})
        spec = AppointmentInitSpec.model_validate({"slot_id": "s", "patient": PATIENT})
        self.assertEqual(spec.params(), {"slot_id": "s", "patient": PATIENT})

    def test_confirm_adds_the_optional_coordination_ref(self):
        plain = AppointmentConfirmSpec.model_validate({"slot_id": "s", "patient": PATIENT})
        linked = AppointmentConfirmSpec.model_validate(
            {"slot_id": "s", "patient": PATIENT, "coordination_ref": "ref-1"}
        )
        self.assertEqual(plain.params(), {"slot_id": "s", "patient": PATIENT})
        self.assertEqual(linked.params(), {"slot_id": "s", "patient": PATIENT, "coordination_ref": "ref-1"})

    def test_cancel_reason_is_optional(self):
        self.assertIsNone(AppointmentCancelSpec.model_validate({}).reason)
        self.assertEqual(AppointmentCancelSpec.model_validate({"reason": "unwell"}).reason, "unwell")

    def test_reschedule_requires_a_slot(self):
        with self.assertRaisesMessage(ValidationError, "slot_id is required"):
            AppointmentRescheduleSpec.model_validate({})
        self.assertEqual(AppointmentRescheduleSpec.model_validate({"slot_id": "s"}).slot_id, "s")

    def test_extra_fields_are_forbidden(self):
        for spec, body in (
            (AppointmentSelectSpec, {"resource_id": "r", "bogus": 1}),
            (AppointmentInitSpec, {"slot_id": "s", "patient": PATIENT, "bogus": 1}),
            (AppointmentConfirmSpec, {"slot_id": "s", "patient": PATIENT, "bogus": 1}),
            (AppointmentCancelSpec, {"bogus": 1}),
            (AppointmentRescheduleSpec, {"slot_id": "s", "bogus": 1}),
        ):
            with self.assertRaises(ValidationError, msg=spec.__name__):
                spec.model_validate(body)

    def test_context_overrides_are_accepted(self):
        context = {
            "network_id": "nfh-test",
            "bap_id": "bap.x",
            "bap_uri": "http://bap.x",
            "bpp_id": "bpp.x",
            "bpp_uri": "http://bpp.x",
            "unknown": "ignored",
        }
        spec = AppointmentCancelSpec.model_validate({"context": context})
        self.assertEqual(spec.context.bpp_id, "bpp.x")
        self.assertEqual(
            spec.context_overrides(),
            {
                "networkId": "nfh-test",
                "bapId": "bap.x",
                "bapUri": "http://bap.x",
                "bppId": "bpp.x",
                "bppUri": "http://bpp.x",
            },
        )
        self.assertNotIn("context", spec.params())
