import uuid

from django.test import SimpleTestCase
from pydantic import ValidationError

from care_ccn_be.resources.referral.spec import ReferralConfirmSpec, ReferralInitSpec, ReferralSelectSpec

FACILITY = str(uuid.uuid4())


class SpecTests(SimpleTestCase):
    def test_bodies_are_snake_case(self):
        spec = ReferralConfirmSpec.model_validate(
            {"facility": FACILITY, "patient_name": "A", "assigned_facility_id": "x"}
        )
        self.assertEqual((spec.patient_name, spec.assigned_facility_id), ("A", "x"))
        with self.assertRaises(ValidationError):
            ReferralConfirmSpec.model_validate({"facility": FACILITY, "patientName": "A"})

    def test_extra_fields_are_rejected(self):
        with self.assertRaises(ValidationError):
            ReferralInitSpec.model_validate({"facility": FACILITY, "bogus": 1})

    def test_facility_is_required_without_a_passthrough_message(self):
        with self.assertRaises(ValidationError):
            ReferralConfirmSpec.model_validate({})
        with self.assertRaises(ValidationError):
            ReferralConfirmSpec.model_validate({"message": {"contract": {}}})

    def test_confirm_passthrough_requires_a_facility(self):
        ReferralConfirmSpec.model_validate(
            {"message": {"contract": {"contractAttributes": {"facilityId": "facility-1"}}}}
        )

    def test_select_needs_an_offer(self):
        with self.assertRaises(ValidationError) as caught:
            ReferralSelectSpec.model_validate({})
        self.assertIn("offer_id is required", str(caught.exception))
        self.assertEqual(ReferralSelectSpec.model_validate({"offer_id": "o"}).offer_id, "o")

    def test_select_offer_from_a_contract_fragment(self):
        spec = ReferralSelectSpec.model_validate({"message": {"contract": {"commitments": [{"offer": {"id": "o-1"}}]}}})
        self.assertIsNotNone(spec.message)

    def test_params_exclude_context_and_empty_values(self):
        context = {"network_id": "n", "bap_id": "a", "bap_uri": "http://a", "bpp_id": "b", "bpp_uri": "http://b"}
        spec = ReferralInitSpec.model_validate({"facility": FACILITY, "title": "T", "context": context})
        self.assertEqual(spec.params(), {"facility": FACILITY, "title": "T", "emergency": False})
