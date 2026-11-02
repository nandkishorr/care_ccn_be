from care.emr.models import Encounter
from model_bakery import baker

from care_ccn_be.flows.referral import builders
from care_ccn_be.flows.referral.builders import CareReferral, ReferralCareConfirmBuilder, ReferralEcho
from care_ccn_be.services import availability as availabilities
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
    def build(self, referral, **details):
        return ReferralCareConfirmBuilder().message(CareReferral(referral, str(referral.external_id), details))[
            "contract"
        ]

    def test_target_and_origin_facility_ids(self):
        patient = self.create_patient(name="Asha", gender="female")
        referral = self.create_referral(
            availability=self.network_availability(assigned=self.target), patient=patient, title="Heart", note="pain"
        )
        encounter = baker.make(Encounter, patient=patient, facility=self.origin)
        referral.encounter = encounter
        contract = self.build(referral, urgency="URGENT")
        attributes = contract["contractAttributes"]
        self.assertEqual(attributes["@type"], "hrf:HealthReferral")
        self.assertEqual(attributes["coordinationId"], str(referral.external_id))
        self.assertEqual(attributes["lifecycleState"], "ACTIVE")
        self.assertEqual(attributes["clinicalUrgencyTier"], "URGENT")
        self.assertEqual(attributes["facilityId"], str(self.origin.external_id))
        self.assertEqual(attributes["originFacilityId"], str(self.origin.external_id))
        self.assertEqual(attributes["assignedFacilityId"], str(self.target.external_id))
        self.assertEqual(contract["status"]["code"], "ACTIVE")
        self.assertEqual(contract["descriptor"], {"name": "Heart", "shortDesc": "pain"})
        (commitment,) = contract["commitments"]
        self.assertEqual(commitment["offer"]["id"], f"offer-{referral.external_id}")
        (participant,) = contract["participants"]
        self.assertEqual(participant["id"], f"participant-patient-{patient.external_id}")
        self.assertEqual(participant["participantAttributes"]["participantRole"], "PATIENT")
        self.assertEqual(participant["participantAttributes"]["gender"], "FEMALE")

    def test_assigned_facility_may_belong_to_the_other_instance(self):
        referral = self.create_referral(
            availability=availabilities.network("cc.bpp", "http://cc.bpp", "lab", "remote-1")
        )
        attributes = self.build(referral)["contractAttributes"]
        self.assertEqual(attributes["assignedFacilityId"], "remote-1")
        self.assertEqual(attributes["facilityId"], "remote-1")

    def test_the_origin_facility_is_remembered_without_an_encounter(self):
        attributes = self.build(self.create_referral(), originFacilityId="fac-1")["contractAttributes"]
        self.assertEqual(attributes["facilityId"], "fac-1")

    def test_unknown_patient_gender_is_reported_as_other(self):
        referral = self.create_referral(patient=self.create_patient(name="X", gender="transgender"))
        self.assertEqual(self.build(referral)["participants"][0]["participantAttributes"]["gender"], "OTHER")

    def test_urgency_defaults_to_routine(self):
        self.assertEqual(self.build(self.create_referral())["contractAttributes"]["clinicalUrgencyTier"], "ROUTINE")
        urgent = self.build(self.create_referral(), urgency="EMERGENCY")
        self.assertEqual(urgent["contractAttributes"]["clinicalUrgencyTier"], "EMERGENCY")

    def test_referral_type_drives_the_target_criteria(self):
        consultation = self.build(self.create_referral())["contractAttributes"]["targetCriteria"]
        self.assertEqual(consultation["serviceCategory"]["code"], "CONSULTATION")
        lab = self.build(self.create_referral(availability=self.network_availability("lab")))
        self.assertEqual(lab["contractAttributes"]["targetCriteria"]["serviceCategory"]["code"], "INVESTIGATION")


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
        referral = self.create_referral(patient=patient, status="in_progress", title="T", note="why")
        details = {"urgency": "URGENT", "originFacilityId": str(self.origin.external_id)}
        init = ReferralEcho.on_init(self.message(), referral, details)["contract"]
        confirm = ReferralEcho.on_confirm(self.message(), referral, details)["contract"]
        status = ReferralEcho.on_status(self.message(), referral, details)["contract"]
        self.assertEqual(init["status"]["code"], "DRAFT")
        self.assertEqual(confirm["status"]["code"], "ACTIVE")
        self.assertEqual(status["status"]["code"], "ACTIVE")
        self.assertEqual(confirm["id"], str(referral.external_id))
        attributes = confirm["contractAttributes"]
        self.assertEqual(attributes["lifecycleState"], "ACTIVE")
        block = attributes["referral"]
        self.assertEqual(
            (block["id"], block["reason"], block["referralType"]), (str(referral.external_id), "why", "consultation")
        )
        self.assertEqual((block["emergency"], block["priority"]), (False, 2))
        self.assertEqual(block["patient"]["gender"], "MALE")
        self.assertEqual(block["originFacilityId"], str(self.origin.external_id))

    def test_status_reports_pending_as_draft_and_completed_as_completed(self):
        pending = ReferralEcho.on_status(self.message(), self.create_referral())["contract"]
        self.assertEqual(pending["status"]["code"], "DRAFT")
        done = ReferralEcho.on_status(self.message(), self.create_referral(status="completed"))["contract"]
        self.assertEqual(done["status"]["code"], "COMPLETED")
        self.assertEqual(done["contractAttributes"]["lifecycleState"], "FULFILLED")

    def test_cancelled_and_rejected_are_cancelled(self):
        for status in ("cancelled", "rejected"):
            body = ReferralEcho.on_status(self.message(), self.create_referral(status=status))["contract"]
            self.assertEqual(body["status"]["code"], "CANCELLED")

    def test_existing_contract_id_is_kept(self):
        message = self.message()
        message["contract"]["id"] = "keep-me"
        body = ReferralEcho.on_confirm(message, self.create_referral())["contract"]
        self.assertEqual(body["id"], "keep-me")

    def test_completion_message(self):
        contract = builders.completion_message("coord-1")["contract"]
        self.assertEqual(contract["status"]["code"], "COMPLETED")
        self.assertEqual(contract["contractAttributes"]["lifecycleState"], "FULFILLED")
        self.assertEqual(contract["contractAttributes"]["coordinationId"], "coord-1")
