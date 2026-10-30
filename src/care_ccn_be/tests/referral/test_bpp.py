from care.emr.models import Patient
from care.emr.models.patient import PatientIdentifier
from care.emr.models.resource_request import ResourceRequest

from care_ccn_be.flows.referral import lifecycle
from care_ccn_be.flows.referral.models import ReferralLink
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

    def test_init_creates_a_pending_origin_request_and_patient(self):
        message = self.referral_message(facility=self.origin)
        self.assertEqual(self.ack(self.request("init", "coord-1", message)), "ACK")
        request = ResourceRequest.objects.get()
        self.assertEqual(request.status, "pending")
        self.assertEqual(
            (request.origin_facility, request.title, request.reason), (self.origin, "Cardiology referral", "chest pain")
        )
        self.assertEqual((request.priority, request.emergency), (2, False))
        patient = Patient.objects.get(name="Asha Rao")
        self.assertEqual(request.related_patient, patient)
        self.assertEqual(patient.gender, "female")
        self.assertTrue(PatientIdentifier.objects.filter(patient=patient, value="91-1111-2222-3333").exists())
        (callback,) = self.callbacks("on_init")
        contract = callback["message"]["contract"]
        self.assertEqual(contract["status"]["code"], "DRAFT")
        self.assertEqual(contract["id"], str(request.external_id))
        self.assertEqual(contract["contractAttributes"]["lifecycleState"], "DRAFT")
        self.assertEqual(contract["contractAttributes"]["referral"]["patient"]["name"], "Asha Rao")

    def test_init_for_an_assigned_only_facility_creates_the_assigned_request(self):
        self.request("init", "coord-1", self.referral_message(assigned=self.target))
        request = ResourceRequest.objects.get()
        self.assertEqual((request.origin_facility, request.assigned_facility), (self.target, self.target))
        self.assertEqual(ReferralLink.objects.get().role, "assigned")

    def test_init_without_a_resolvable_facility_is_an_error_callback(self):
        self.request("init", "coord-1", self.referral_message(facility=None, assigned=None))
        (callback,) = self.callbacks("on_init")
        self.assertIn("No facility id", callback["error"]["message"])
        self.assertFalse(ResourceRequest.objects.exists())

    def test_repeated_init_and_confirm_never_duplicate_requests(self):
        message = self.referral_message(facility=self.origin, assigned=self.target)
        self.request("init", "coord-1", message, message_id="m-1")
        self.request("init", "coord-1", message, message_id="m-2")
        self.request("confirm", "coord-1", message, message_id="m-3")
        self.request("confirm", "coord-1", message, message_id="m-4")
        self.assertEqual(ResourceRequest.objects.filter(extensions__beckn__role="origin").count(), 1)
        self.assertEqual(ResourceRequest.objects.filter(extensions__beckn__role="assigned").count(), 1)
        self.assertEqual(ReferralLink.objects.count(), 2)
        self.assertEqual(Patient.objects.filter(name="Asha Rao").count(), 1)

    def test_confirm_approves_the_origin_and_creates_the_approved_assigned_request(self):
        message = self.referral_message(facility=self.origin, assigned=self.target)
        self.request("init", "coord-1", message)
        self.request("confirm", "coord-1", message, message_id="m-2")
        origin = lifecycle.current("coord-1", lifecycle.ORIGIN)
        assigned = lifecycle.current("coord-1", lifecycle.ASSIGNED)
        self.assertEqual((origin.status, assigned.status), ("approved", "approved"))
        self.assertEqual((assigned.origin_facility, assigned.assigned_facility), (self.origin, self.target))
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
        self.assertEqual(contract["contractAttributes"]["referral"]["status"], "approved")

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
        self.assertEqual(ResourceRequest.objects.count(), 2)

    def test_unsupported_actions_are_nacked(self):
        for action in ("cancel", "update"):
            response = self.request(action, "coord-1", self.referral_message())
            self.assertEqual(self.ack(response), "NACK")

    def test_inbound_referral_is_routed_to_the_flow_and_the_coordination_id_is_stored(self):
        self.request("select", "coord-7", self.referral_message(coordination_id="coord-7"))
        txn = self.bpp_txn_of("coord-7")
        self.assertEqual((txn.flow, txn.coordination_id), ("referral", "coord-7"))
