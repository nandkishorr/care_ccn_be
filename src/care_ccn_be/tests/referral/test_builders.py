from care_ccn_be.flows.referral import builders
from care_ccn_be.flows.referral.builders import CareReferral, ReferralCareConfirmBuilder, ReferralEcho
from care_ccn_be.tests.referral.base import ReferralTestBase


class ServiceCategoryCriteriaTests(ReferralTestBase):
    def test_patient_care_is_a_consultation(self):
        criteria = builders.service_category_criteria("patient_care")
        self.assertEqual(criteria["serviceCategory"]["code"], "CONSULTATION")
        self.assertEqual(criteria["procedureNeeds"], ["HOME_VISIT"])
        self.assertEqual(criteria["consultationModality"], "IN_PERSON")

    def test_other_categories_are_an_investigation(self):
        criteria = builders.service_category_criteria("other")
        self.assertEqual(criteria["serviceCategory"]["code"], "INVESTIGATION")
        self.assertNotIn("healthServiceType", criteria)

    def test_medicines_add_pharmacy_dispensing(self):
        criteria = builders.service_category_criteria("medicines")
        self.assertEqual(criteria["serviceCategory"]["code"], "INVESTIGATION")
        self.assertEqual(criteria["healthServiceType"], "PHARMACY_DISPENSING")


class CareConfirmBuilderTests(ReferralTestBase):
    def build(self, request):
        return ReferralCareConfirmBuilder().message(CareReferral(request, str(request.external_id)))["contract"]

    def test_assigned_and_origin_facility_ids(self):
        patient = self.create_patient(name="Asha", gender="female")
        request = self.create_request(
            assigned_facility=self.target, related_patient=patient, title="Heart", reason="pain", priority=2
        )
        contract = self.build(request)
        attributes = contract["contractAttributes"]
        self.assertEqual(attributes["@type"], "hrf:HealthReferral")
        self.assertEqual(attributes["coordinationId"], str(request.external_id))
        self.assertEqual(attributes["lifecycleState"], "ACTIVE")
        self.assertEqual(attributes["clinicalUrgencyTier"], "URGENT")
        self.assertEqual(attributes["facilityId"], str(self.origin.external_id))
        self.assertEqual(attributes["originFacilityId"], str(self.origin.external_id))
        self.assertEqual(attributes["assignedFacilityId"], str(self.target.external_id))
        self.assertEqual(contract["status"]["code"], "ACTIVE")
        self.assertEqual(contract["descriptor"], {"name": "Heart", "shortDesc": "pain"})
        (commitment,) = contract["commitments"]
        self.assertEqual(commitment["offer"]["id"], f"offer-{request.external_id}")
        (participant,) = contract["participants"]
        self.assertEqual(participant["id"], f"participant-patient-{patient.external_id}")
        self.assertEqual(participant["participantAttributes"]["participantRole"], "PATIENT")
        self.assertEqual(participant["participantAttributes"]["gender"], "FEMALE")

    def test_assigned_facility_comes_from_the_extension_when_remote(self):
        request = self.create_request(extensions={"beckn": {"target": {"assignedFacilityId": "remote-1"}}})
        self.assertEqual(self.build(request)["contractAttributes"]["assignedFacilityId"], "remote-1")

    def test_unknown_patient_gender_is_reported_as_other(self):
        patient = self.create_patient(name="X", gender="transgender")
        request = self.create_request(related_patient=patient)
        self.assertEqual(self.build(request)["participants"][0]["participantAttributes"]["gender"], "OTHER")

    def test_routine_below_the_urgency_threshold(self):
        request = self.create_request(priority=1, emergency=False)
        self.assertEqual(self.build(request)["contractAttributes"]["clinicalUrgencyTier"], "ROUTINE")
        request = self.create_request(emergency=True)
        self.assertEqual(self.build(request)["contractAttributes"]["clinicalUrgencyTier"], "EMERGENCY")

    def test_no_patient_means_no_participants(self):
        self.assertNotIn("participants", self.build(self.create_request()))

    def test_category_drives_the_target_criteria(self):
        request = self.create_request(category="patient_care")
        criteria = self.build(request)["contractAttributes"]["targetCriteria"]
        self.assertEqual(criteria["serviceCategory"]["code"], "CONSULTATION")


class EchoTests(ReferralTestBase):
    def message(self):
        return {
            "contract": {
                "consideration": {"x": 1},
                "quote": {"y": 2},
                "commitments": [{"id": "c", "considerations": [1], "offer": {"id": "o", "considerations": [2]}}],
                "contractAttributes": {"@type": "hrf:HealthReferral"},
            }
        }

    def test_select_strips_pricing_and_is_a_draft(self):
        body = ReferralEcho.on_select(self.message())["contract"]
        self.assertEqual(body["status"]["code"], "DRAFT")
        self.assertNotIn("consideration", body)
        self.assertNotIn("quote", body)
        self.assertNotIn("considerations", body["commitments"][0])
        self.assertNotIn("considerations", body["commitments"][0]["offer"])

    def test_overlay_statuses_and_referral_block(self):
        patient = self.create_patient(name="Asha", gender="male")
        request = self.create_request(related_patient=patient, status="approved", title="T")
        init = ReferralEcho.on_init(self.message(), request)["contract"]
        confirm = ReferralEcho.on_confirm(self.message(), request)["contract"]
        status = ReferralEcho.on_status(self.message(), request)["contract"]
        self.assertEqual(init["status"]["code"], "DRAFT")
        self.assertEqual(confirm["status"]["code"], "ACTIVE")
        self.assertEqual(status["status"]["code"], "ACTIVE")
        self.assertEqual(confirm["id"], str(request.external_id))
        attributes = confirm["contractAttributes"]
        self.assertEqual(attributes["lifecycleState"], "ACTIVE")
        self.assertEqual(attributes["referral"]["id"], str(request.external_id))
        self.assertEqual(attributes["referral"]["patient"]["gender"], "MALE")
        self.assertEqual(attributes["referral"]["originFacilityId"], str(self.origin.external_id))

    def test_status_reports_pending_as_draft_and_completed_as_completed(self):
        pending = ReferralEcho.on_status(self.message(), self.create_request())["contract"]
        self.assertEqual(pending["status"]["code"], "DRAFT")
        done = ReferralEcho.on_status(self.message(), self.create_request(status="completed"))["contract"]
        self.assertEqual(done["status"]["code"], "COMPLETED")
        self.assertEqual(done["contractAttributes"]["lifecycleState"], "FULFILLED")

    def test_existing_contract_id_is_kept(self):
        message = self.message()
        message["contract"]["id"] = "keep-me"
        body = ReferralEcho.on_confirm(message, self.create_request())["contract"]
        self.assertEqual(body["id"], "keep-me")

    def test_completion_message(self):
        contract = builders.completion_message("coord-1")["contract"]
        self.assertEqual(contract["status"]["code"], "COMPLETED")
        self.assertEqual(contract["contractAttributes"]["lifecycleState"], "FULFILLED")
        self.assertEqual(contract["contractAttributes"]["coordinationId"], "coord-1")
