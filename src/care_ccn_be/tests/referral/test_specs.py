from django.test import SimpleTestCase
from pydantic import ValidationError

from care_ccn_be.flows.referral.specs import ReferralConfirmSpec, ReferralInitSpec, ReferralSelectSpec


class SpecTests(SimpleTestCase):
    def test_camel_and_snake_case_are_accepted(self):
        camel = ReferralConfirmSpec.model_validate({"facility": "f", "patientName": "A", "assignedFacilityId": "x"})
        snake = ReferralConfirmSpec.model_validate({"facility": "f", "patient_name": "A", "assigned_facility_id": "x"})
        self.assertEqual(camel, snake)

    def test_extra_fields_are_rejected(self):
        with self.assertRaises(ValidationError):
            ReferralInitSpec.model_validate({"facility": "f", "bogus": 1})

    def test_facility_is_required_without_a_passthrough_message(self):
        with self.assertRaises(ValidationError):
            ReferralConfirmSpec.model_validate({})
        ReferralConfirmSpec.model_validate({"message": {"contract": {}}})

    def test_select_needs_an_offer(self):
        with self.assertRaises(ValidationError) as caught:
            ReferralSelectSpec.model_validate({})
        self.assertIn("offerId is required", str(caught.exception))
        self.assertEqual(ReferralSelectSpec.model_validate({"offerId": "o"}).offer_id, "o")

    def test_select_offer_from_a_contract_fragment(self):
        spec = ReferralSelectSpec.model_validate({"contract": {"offers": [{"id": "o-1"}]}})
        self.assertEqual(spec.offer_id, "o-1")

    def test_params_exclude_context_and_empty_values(self):
        spec = ReferralInitSpec.model_validate({"facility": "f", "title": "T", "context": {"bppId": "x"}})
        self.assertEqual(spec.params(), {"facility": "f", "title": "T", "emergency": False})
