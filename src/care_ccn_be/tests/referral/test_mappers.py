from datetime import date

from django.test import SimpleTestCase

from care_ccn_be.services import mappers


class GenderTests(SimpleTestCase):
    def test_known_genders(self):
        self.assertEqual(mappers.map_gender("MALE"), "male")
        self.assertEqual(mappers.map_gender("female"), "female")
        self.assertEqual(mappers.map_gender_to_nfh("male"), "MALE")

    def test_other_unknown_and_missing_map_to_non_binary(self):
        for value in ("OTHER", "PREFER_NOT_TO_SAY", "TRANSGENDER", "wat", "", None):
            self.assertEqual(mappers.map_gender(value), "non_binary", value)

    def test_care_genders_without_an_nfh_value_map_to_other(self):
        for value in ("non_binary", "transgender", "", None, "wat"):
            self.assertEqual(mappers.map_gender_to_nfh(value), "OTHER", value)


class UrgencyTests(SimpleTestCase):
    def test_emergency_wins(self):
        self.assertEqual(mappers.urgency_tier(True, 1), "EMERGENCY")

    def test_priority_at_or_above_the_threshold_is_urgent(self):
        self.assertEqual(mappers.urgency_tier(False, 2), "URGENT")
        self.assertEqual(mappers.urgency_tier(False, 5), "URGENT")

    def test_below_the_threshold_is_routine(self):
        self.assertEqual(mappers.urgency_tier(False, 1), "ROUTINE")
        self.assertEqual(mappers.urgency_tier(False, 0), "ROUTINE")
        self.assertEqual(mappers.urgency_tier(False, None), "ROUTINE")


class ContractHelpersTests(SimpleTestCase):
    message = {
        "contract": {
            "descriptor": {"name": "Heart"},
            "participants": [
                {
                    "id": "p1",
                    "descriptor": {"name": "Dr Who"},
                    "participantAttributes": {"participantRole": "REFERRER", "telecom": "+91 98765 43210123"},
                },
                {
                    "id": "p2",
                    "participantAttributes": {
                        "participantRole": "PATIENT",
                        "dateOfBirth": "1990-02-03",
                        "healthIds": [{"system": "ABHA", "value": "1"}, {"system": "X"}],
                    },
                },
            ],
            "contractAttributes": {"clinicalUrgencyTier": "URGENT", "consent": {"clinicalJustification": "pain"}},
        }
    }

    def test_patient_participant_health_ids_and_dob(self):
        participant = mappers.find_patient_participant(self.message)
        self.assertEqual(participant["id"], "p2")
        self.assertEqual(mappers.extract_health_ids(participant), [{"system": "ABHA", "value": "1"}])
        self.assertEqual(mappers.extract_date_of_birth(participant), date(1990, 2, 3))

    def test_referral_fields(self):
        fields = mappers.referral_fields(self.message)
        self.assertEqual(fields["title"], "Heart")
        self.assertEqual(fields["note"], "pain")
        self.assertEqual((fields["urgency"], fields["referral_type"]), ("URGENT", "consultation"))
        self.assertEqual(fields["referring_facility_contact_name"], "Dr Who")
        self.assertEqual(len(fields["referring_facility_contact_number"]), 14)

    def test_empty_message_is_safe(self):
        self.assertIsNone(mappers.find_patient_participant({}))
        self.assertEqual(mappers.referral_fields(None)["title"], "NFH Referral")
        self.assertEqual(mappers.referral_fields(None)["urgency"], "ROUTINE")

    def test_unknown_urgency_is_routine(self):
        message = {"contract": {"contractAttributes": {"clinicalUrgencyTier": "WHENEVER"}}}
        self.assertEqual(mappers.referral_fields(message)["urgency"], "ROUTINE")

    def test_investigations_are_lab_referrals_everything_else_a_consultation(self):
        def message(code):
            return {"contract": {"contractAttributes": {"targetCriteria": {"serviceCategory": {"code": code}}}}}

        self.assertEqual(mappers.referral_type(message("INVESTIGATION")), "lab")
        self.assertEqual(mappers.referral_type(message("CONSULTATION")), "consultation")
        self.assertEqual(mappers.referral_type({}), "consultation")

    def test_status_to_lifecycle(self):
        self.assertEqual(mappers.map_status_to_lifecycle("pending"), "DRAFT")
        self.assertEqual(mappers.map_status_to_lifecycle("in_progress"), "ACTIVE")
        self.assertEqual(mappers.map_status_to_lifecycle("completed"), "FULFILLED")
        self.assertEqual(mappers.map_status_to_lifecycle("cancelled"), "CANCELLED")
        self.assertEqual(mappers.map_status_to_lifecycle("rejected"), "CANCELLED")
        self.assertEqual(mappers.map_status_to_lifecycle("nonsense"), "DRAFT")
