import datetime

from django.test import SimpleTestCase
from pydantic import ValidationError

from care_ccn_be.flows.appointment.specs import (
    AppointmentCancelSpec,
    AppointmentConfirmSpec,
    AppointmentInitSpec,
    AppointmentRescheduleSpec,
    AppointmentSelectSpec,
)


class SpecTests(SimpleTestCase):
    def test_select_accepts_camel_and_snake_case(self):
        camel = AppointmentSelectSpec.model_validate({"offerId": "o", "resourceId": "r", "date": "2026-07-01"})
        snake = AppointmentSelectSpec.model_validate({"offer_id": "o", "resource_id": "r", "date": "2026-07-01"})
        self.assertEqual(camel, snake)
        self.assertEqual((camel.resource_id, camel.offer_id, camel.date), ("r", "o", datetime.date(2026, 7, 1)))

    def test_select_requires_a_resource_unless_a_message_is_passed_through(self):
        with self.assertRaisesMessage(ValidationError, "resourceId is required"):
            AppointmentSelectSpec.model_validate({"date": "2026-07-01"})
        self.assertIsNone(AppointmentSelectSpec.model_validate({"message": {"contract": {}}}).resource_id)

    def test_select_rejects_a_bad_date(self):
        with self.assertRaises(ValidationError):
            AppointmentSelectSpec.model_validate({"resourceId": "r", "date": "tomorrow"})

    def test_init_requires_slot_and_patient(self):
        with self.assertRaisesMessage(ValidationError, "slotId is required"):
            AppointmentInitSpec.model_validate({"patient": "p"})
        with self.assertRaisesMessage(ValidationError, "patient is required"):
            AppointmentInitSpec.model_validate({"slotId": "s"})
        spec = AppointmentInitSpec.model_validate({"slot_id": "s", "patient": "p"})
        self.assertEqual(spec.params(), {"slotId": "s", "patient": "p"})

    def test_confirm_adds_the_optional_coordination_ref(self):
        plain = AppointmentConfirmSpec.model_validate({"slotId": "s", "patient": "p"})
        linked = AppointmentConfirmSpec.model_validate({"slotId": "s", "patient": "p", "coordinationRef": "ref-1"})
        self.assertEqual(plain.params(), {"slotId": "s", "patient": "p"})
        self.assertEqual(linked.params(), {"slotId": "s", "patient": "p", "coordinationRef": "ref-1"})

    def test_cancel_reason_is_optional(self):
        self.assertIsNone(AppointmentCancelSpec.model_validate({}).reason)
        self.assertEqual(AppointmentCancelSpec.model_validate({"reason": "unwell"}).reason, "unwell")

    def test_reschedule_requires_a_slot(self):
        with self.assertRaisesMessage(ValidationError, "slotId is required"):
            AppointmentRescheduleSpec.model_validate({})
        self.assertEqual(AppointmentRescheduleSpec.model_validate({"slot_id": "s"}).slot_id, "s")

    def test_extra_fields_are_forbidden(self):
        for spec, body in (
            (AppointmentSelectSpec, {"resourceId": "r", "bogus": 1}),
            (AppointmentInitSpec, {"slotId": "s", "patient": "p", "bogus": 1}),
            (AppointmentConfirmSpec, {"slotId": "s", "patient": "p", "bogus": 1}),
            (AppointmentCancelSpec, {"bogus": 1}),
            (AppointmentRescheduleSpec, {"slotId": "s", "bogus": 1}),
        ):
            with self.assertRaises(ValidationError, msg=spec.__name__):
                spec.model_validate(body)

    def test_context_overrides_are_accepted(self):
        spec = AppointmentCancelSpec.model_validate({"context": {"bppId": "x"}})
        self.assertEqual(spec.context, {"bppId": "x"})
