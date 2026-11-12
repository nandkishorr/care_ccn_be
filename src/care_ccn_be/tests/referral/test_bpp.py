from care.emr.models import Patient
from care.emr.models.patient import PatientIdentifier
from care.emr.models.referral import Referral

from care_ccn_be.models.referral import ReferralLink
from care_ccn_be.services import availability as availabilities
from care_ccn_be.services import referral as lifecycle
from care_ccn_be.tests.referral.base import ReferralTestBase


class BppTests(ReferralTestBase):
    def ack(self, response):
        return response.json()["message"]["ack"]["status"]

    def callbacks(self, action):
        return [p for _, a, p in self.sent if a == action]

    def setUp(self):
        super().setUp()
        self.stub_delivery()

    def test_select_echoes_a_draft_without_pricing(self):
        message = self.referral_message()
        message["contract"]["consideration"] = {"price": 1}
        self.assertEqual(self.ack(self.request("select", "coord-1", message)), "ACK")
        (callback,) = self.callbacks("on_select")
        self.assertEqual(callback["message"]["contract"]["status"]["code"], "DRAFT")
        self.assertNotIn("consideration", callback["message"]["contract"])
        self.assertEqual(callback["context"]["messageId"], "m-1")

    def test_init_creates_a_pending_origin_referral_and_patient(self):
        message = self.referral_message(facility=self.origin)
        self.assertEqual(self.ack(self.request("init", "coord-1", message)), "ACK")
        referral = Referral.objects.get()
        self.assertEqual(referral.status, "pending")
        self.assertEqual((referral.title, referral.note), ("Cardiology referral", "chest pain"))
        availability = referral.availability
        self.assertEqual(availability.entity_type, "external")
        self.assertEqual(availabilities.target_of(availability)["bppId"], "cc.bpp")
        self.assertEqual(availability.referral_type, "consultation")
        link = ReferralLink.objects.get()
        self.assertEqual(link.role, "origin")
        self.assertEqual(link.details, {"urgency": "URGENT", "originFacilityId": str(self.origin.external_id)})
        patient = Patient.objects.get(name="Asha Rao")
        self.assertEqual(referral.patient, patient)
        self.assertEqual(patient.gender, "female")
        self.assertTrue(PatientIdentifier.objects.filter(patient=patient, value="91-1111-2222-3333").exists())
        (callback,) = self.callbacks("on_init")
        contract = callback["message"]["contract"]
        self.assertEqual(contract["status"]["code"], "DRAFT")
        self.assertEqual(contract["id"], str(referral.external_id))
        self.assertEqual(contract["contractAttributes"]["lifecycleState"], "DRAFT")
        self.assertEqual(contract["contractAttributes"]["referral"]["patient"]["name"], "Asha Rao")
        self.assertEqual(contract["contractAttributes"]["referral"]["priority"], 2)

    def test_init_for_an_assigned_only_facility_creates_the_assigned_referral(self):
        self.request("init", "coord-1", self.referral_message(assigned=self.target))
        referral = Referral.objects.get()
        availability = referral.availability
        self.assertEqual((availability.entity_type, availability.facility), ("facility", self.target))
        self.assertEqual(ReferralLink.objects.get().role, "assigned")

    def test_a_lab_referral_lands_on_a_lab_availability(self):
        criteria = {"serviceCategory": {"code": "INVESTIGATION"}}
        message = self.referral_message(assigned=self.target, targetCriteria=criteria)
        self.request("init", "coord-1", message)
        self.assertEqual(Referral.objects.get().availability.referral_type, "lab")

    def test_the_assigned_referral_is_assigned_to_the_availability_care_team(self):
        availability = availabilities.for_facility(self.target)
        member = {"user_id": self.user.id, "role": {"code": "doctor"}}
        availability.care_team = [member]
        availability.save()
        self.request("init", "coord-1", self.referral_message(assigned=self.target))
        referral = Referral.objects.get()
        self.assertEqual(referral.availability, availability)
        self.assertEqual((referral.assignees, referral.assignee_users), ([member], [self.user.id]))

    def test_init_without_a_resolvable_facility_is_an_error_callback(self):
        self.request("init", "coord-1", self.referral_message(facility=None, assigned=None))
        (callback,) = self.callbacks("on_init")
        self.assertIn("No facility id", callback["error"]["message"])
        self.assertFalse(Referral.objects.exists())

    def test_init_without_a_patient_is_an_error_callback(self):
        self.request("init", "coord-1", self.referral_message(facility=self.origin, patient=False))
        (callback,) = self.callbacks("on_init")
        self.assertIn("No patient participant", callback["error"]["message"])
        self.assertFalse(Referral.objects.exists())

    def test_repeated_init_and_confirm_never_duplicate_referrals(self):
        message = self.referral_message(facility=self.origin, assigned=self.target)
        self.request("init", "coord-1", message, message_id="m-1")
        self.request("init", "coord-1", message, message_id="m-2")
        self.request("confirm", "coord-1", message, message_id="m-3")
        self.request("confirm", "coord-1", message, message_id="m-4")
        self.assertEqual(ReferralLink.objects.filter(role="origin").count(), 1)
        self.assertEqual(ReferralLink.objects.filter(role="assigned").count(), 1)
        self.assertEqual(Referral.objects.count(), 2)
        self.assertEqual(Patient.objects.filter(name="Asha Rao").count(), 1)

    def test_confirm_approves_the_origin_and_creates_the_in_progress_assigned_referral(self):
        message = self.referral_message(facility=self.origin, assigned=self.target)
        self.request("init", "coord-1", message)
        self.request("confirm", "coord-1", message, message_id="m-2")
        origin = lifecycle.current("coord-1", lifecycle.ORIGIN)
        assigned = lifecycle.current("coord-1", lifecycle.ASSIGNED)
        self.assertEqual((origin.status, assigned.status), ("in_progress", "in_progress"))
        self.assertEqual(assigned.availability.facility, self.target)
        self.assertEqual(
            availabilities.target_of(origin.availability)["assignedFacilityId"], str(self.target.external_id)
        )
        contract = self.callbacks("on_confirm")[0]["message"]["contract"]
        self.assertEqual(contract["status"]["code"], "ACTIVE")
        self.assertEqual(contract["id"], str(assigned.external_id))

    def test_confirm_without_any_facility_is_an_error_callback(self):
        self.request("confirm", "coord-1", self.referral_message())
        self.assertIn("No facility id", self.callbacks("on_confirm")[0]["error"]["message"])

    def test_status_reports_the_current_state(self):
        message = self.referral_message(facility=self.origin)
        self.request("confirm", "coord-1", message)
        self.request("status", "coord-1", {"contract": {"id": "x"}}, message_id="m-2")
        contract = self.callbacks("on_status")[0]["message"]["contract"]
        self.assertEqual(contract["status"]["code"], "ACTIVE")
        self.assertEqual(contract["contractAttributes"]["referral"]["status"], "in_progress")

    def test_status_for_an_unknown_referral_is_an_error_callback(self):
        self.request("select", "coord-9", self.referral_message(coordination_id="coord-9"))
        self.request("status", "coord-9", {"contract": {"id": "x"}}, message_id="m-2")
        self.assertIn("Referral not found", self.callbacks("on_status")[0]["error"]["message"])

    def test_unknown_gender_creates_a_non_binary_patient(self):
        message = self.referral_message(facility=self.origin, patient={"gender": "ALIEN"})
        self.request("init", "coord-1", message)
        self.assertEqual(Patient.objects.get(name="Asha Rao").gender, "non_binary")

    def test_an_existing_patient_is_matched_by_health_id(self):
        self.request("init", "coord-1", self.referral_message(facility=self.origin))
        self.request(
            "init", "coord-2", self.referral_message(coordination_id="coord-2", facility=self.origin), message_id="m-2"
        )
        self.assertEqual(Patient.objects.filter(name="Asha Rao").count(), 1)
        self.assertEqual(Referral.objects.count(), 2)

    def test_unsupported_actions_are_nacked(self):
        for action in ("cancel", "update"):
            response = self.request(action, "coord-1", self.referral_message())
            self.assertEqual(self.ack(response), "NACK")

    def test_inbound_referral_is_routed_to_the_flow_and_the_coordination_id_is_stored(self):
        self.request("select", "coord-7", self.referral_message(coordination_id="coord-7"))
        txn = self.bpp_txn_of("coord-7")
        self.assertEqual((txn.flow, txn.coordination_id), ("referral", "coord-7"))
