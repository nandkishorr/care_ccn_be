from care.emr.models.resource_request import ResourceRequest
from django.urls import Resolver404, resolve

from care_ccn_be.flows.referral import lifecycle
from care_ccn_be.flows.registry import FlowRegistry
from care_ccn_be.models import BecknMessage, Direction
from care_ccn_be.tests.referral.base import ReferralTestBase

ROOT_URLCONF = "care_ccn_be.tests.referral.urls"


class FlowAdapterTests(ReferralTestBase):
    def test_registry_lookup_includes_the_legacy_aliases(self):
        flow = FlowRegistry.get("referral")
        self.assertIs(FlowRegistry.get("consultation"), flow)
        self.assertIs(FlowRegistry.get("coordination"), flow)
        self.assertEqual(flow.legacy_service_type, "consultation")

    def sent_payload(self, action):
        return next(p for _, a, p in reversed(self.sent) if a == action)

    def test_discover_defaults_to_the_service_coordination_schema_without_a_bpp(self):
        self.stub_delivery()
        self.bap(body={"healthServiceType": "CONSULTATION"})
        context = self.sent_payload("discover")["context"]
        self.assertEqual(
            context["schemaContext"], ["https://schema.beckn.io/ServiceCoordinationResource/v2.1/context.jsonld"]
        )
        self.assertNotIn("bppId", context)

    def test_select_requires_an_offer_id(self):
        txn_id = self.new_transaction()
        response = self.bap(txn_id, "select", {})
        self.assertEqual(response.status_code, 400)
        self.assertIn("offerId is required", str(response.json()))

    def test_select_payload_is_a_draft_with_the_offer(self):
        txn_id = self.new_transaction()
        self.assertEqual(self.bap(txn_id, "select", {"offerId": "offer-1"}).status_code, 202)
        contract = self.sent_payload("select")["message"]["contract"]
        self.assertEqual(contract["status"]["code"], "DRAFT")
        self.assertEqual(contract["commitments"][0]["offer"]["id"], "offer-1")
        self.assertEqual(contract["commitments"][0]["id"], f"commitment-{txn_id}")
        self.assertEqual(contract["contractAttributes"]["coordinationId"], txn_id)
        self.assertEqual(self.poll(txn_id)["actions"][-1], "SELECT")

    def test_confirm_carries_the_patient_name_participant_and_stores_params(self):
        txn_id = self.new_transaction()
        self.bap(txn_id, "select", {"offerId": "offer-1"})
        body = {"facility": str(self.origin.external_id), "patientName": "Asha", "title": "Heart", "reason": "pain"}
        self.assertEqual(self.bap(txn_id, "confirm", body).status_code, 202)
        contract = self.sent_payload("confirm")["message"]["contract"]
        self.assertEqual(contract["status"]["code"], "ACTIVE")
        self.assertEqual(contract["descriptor"], {"name": "Heart", "shortDesc": "pain"})
        self.assertEqual(contract["commitments"][0]["offer"]["id"], "offer-1")
        (participant,) = contract["participants"]
        self.assertEqual(participant["descriptor"]["name"], "Asha")
        self.assertEqual(participant["id"], f"participant-patient-{txn_id}")
        self.assertEqual(contract["contractAttributes"]["facilityId"], str(self.origin.external_id))
        self.assertEqual(self.poll(txn_id)["patient"]["patient_name"], "Asha")

    def test_confirm_without_a_facility_is_a_400(self):
        txn_id = self.new_transaction()
        self.assertEqual(self.bap(txn_id, "confirm", {"title": "x"}).status_code, 400)

    def test_confirm_with_a_care_patient_uses_the_patient_participant(self):
        txn_id = self.new_transaction()
        patient = self.create_patient(name="Ravi", gender="male")
        body = {"facility": str(self.origin.external_id), "patient": str(patient.external_id)}
        self.bap(txn_id, "confirm", body)
        (participant,) = self.sent_payload("confirm")["message"]["contract"]["participants"]
        self.assertEqual(participant["id"], f"participant-patient-{patient.external_id}")
        self.assertEqual(participant["descriptor"]["name"], "Ravi")

    def test_unknown_patient_is_a_404(self):
        txn_id = self.new_transaction()
        body = {"facility": str(self.origin.external_id), "patient": "00000000-0000-0000-0000-000000000000"}
        self.assertEqual(self.bap(txn_id, "confirm", body).status_code, 404)

    def test_routing_overrides_pick_the_bpp(self):
        txn_id = self.new_transaction()
        self.bap(txn_id, "select", {"offerId": "o", "context": {"bppId": "bpp.x", "bppUri": "http://bpp.x"}})
        context = self.sent_payload("select")["context"]
        self.assertEqual((context["bppId"], context["bppUri"]), ("bpp.x", "http://bpp.x"))

    def test_the_default_bpp_is_the_coordination_centre(self):
        txn_id = self.new_transaction()
        self.bap(txn_id, "select", {"offerId": "o"})
        self.assertEqual(self.sent_payload("select")["context"]["bppId"], "cc.bpp")

    def test_passthrough_select_needs_an_offer_and_confirm_a_facility(self):
        txn_id = self.new_transaction()
        self.assertEqual(self.bap(txn_id, "select", {"message": {"contract": {"id": "c"}}}).status_code, 400)
        self.assertEqual(self.bap(txn_id, "confirm", {"message": {"contract": {"id": "c"}}}).status_code, 400)
        select = {"contract": {"commitments": [{"offer": {"id": "o"}}]}}
        self.assertEqual(self.bap(txn_id, "select", {"message": select}).status_code, 202)
        confirm = {"contract": {"contractAttributes": {"facilityId": str(self.origin.external_id)}}}
        self.assertEqual(self.bap(txn_id, "confirm", {"message": confirm}).status_code, 202)
        self.assertEqual(self.poll(txn_id)["patient"]["facility"], str(self.origin.external_id))

    def test_unknown_spec_field_is_a_400(self):
        txn_id = self.new_transaction()
        self.assertEqual(self.bap(txn_id, "init", {"facility": "f", "bogus": 1}).status_code, 400)

    def test_cancel_and_update_are_not_exposed(self):
        for action in ("cancel", "update"):
            with self.assertRaises(Resolver404):
                resolve(f"/bap/referral/txn-1/{action}/", urlconf=ROOT_URLCONF)

    def test_status_sends_the_contract_id(self):
        txn_id = self.new_transaction()
        self.assertEqual(self.bap(txn_id, "status").status_code, 202)
        self.assertEqual(self.sent_payload("status")["message"]["contract"]["id"], txn_id)


class BAPReceiverTests(ReferralTestBase):
    def test_on_discover_records_routing_and_catalogs(self):
        txn_id = self.new_transaction()
        view = self.poll(txn_id, "on_discover")
        self.assertTrue(view["ready"])
        self.assertEqual(view["data"]["message"]["catalogs"][0]["id"], "c")

    def test_on_init_stores_the_payload_and_the_poll_shows_it(self):
        txn_id = self.new_transaction()
        self.bap(txn_id, "init", {"facility": str(self.origin.external_id)})
        message = {"contract": {"id": "c-1", "status": {"code": "DRAFT"}}}
        self.assertEqual(self.callback("on_init", txn_id, message).json()["message"]["ack"]["status"], "ACK")
        view = self.poll(txn_id, "on_init")
        self.assertTrue(view["ready"])
        self.assertEqual(view["data"]["message"], message)
        poll = self.poll(txn_id)
        self.assertEqual(poll["contractId"], "c-1")
        self.assertEqual(poll["routing"]["bppId"], "bpp.remote")

    def test_on_init_creates_the_pending_origin_request(self):
        txn_id = self.new_transaction()
        self.bap(txn_id, "init", {"facility": str(self.origin.external_id), "title": "Heart"})
        self.callback("on_init", txn_id, {"contract": {"id": "c-1", "status": {"code": "DRAFT"}}})
        request = lifecycle.current(txn_id, lifecycle.ORIGIN)
        self.assertEqual((request.status, request.title, request.origin_facility), ("pending", "Heart", self.origin))
        self.assertEqual(self.poll(txn_id)["resourceRequestId"], str(request.external_id))

    def test_on_confirm_creates_the_approved_origin_request_from_the_stored_params(self):
        txn_id = self.new_transaction()
        patient = self.create_patient(name="Ravi")
        body = {
            "facility": str(self.origin.external_id),
            "patient": str(patient.external_id),
            "title": "Heart",
            "reason": "pain",
            "category": "patient_care",
        }
        self.bap(txn_id, "confirm", body)
        message = {"contract": {"id": "c-1", "status": {"code": "ACTIVE"}}}
        self.callback("on_confirm", txn_id, message)
        request = ResourceRequest.objects.get()
        self.assertEqual(request.status, "approved")
        self.assertEqual((request.title, request.reason, request.category), ("Heart", "pain", "patient_care"))
        self.assertEqual((request.origin_facility, request.related_patient), (self.origin, patient))
        self.assertEqual(request.created_by, self.system)

    def test_repeated_on_confirm_never_creates_a_second_origin_request(self):
        txn_id = self.new_transaction()
        self.bap(txn_id, "confirm", {"facility": str(self.origin.external_id)})
        message = {"contract": {"id": "c-1", "status": {"code": "ACTIVE"}}}
        self.callback("on_confirm", txn_id, message, message_id="cb-1")
        self.callback("on_confirm", txn_id, message, message_id="cb-2")
        self.assertEqual(ResourceRequest.objects.count(), 1)

    def test_on_confirm_approves_the_existing_request(self):
        txn_id = self.new_transaction()
        self.bap(txn_id, "init", {"facility": str(self.origin.external_id)})
        self.callback("on_init", txn_id, {"contract": {"id": "c-1", "status": {"code": "DRAFT"}}})
        self.assertEqual(ResourceRequest.objects.get().status, "pending")
        self.bap(txn_id, "confirm", {"facility": str(self.origin.external_id)})
        self.callback("on_confirm", txn_id, {"contract": {"id": "c-1", "status": {"code": "ACTIVE"}}})
        self.assertEqual(ResourceRequest.objects.get().status, "approved")

    def test_on_confirm_without_a_resolvable_facility_marks_the_message_failed(self):
        txn_id = self.new_transaction()
        self.bap(txn_id, "confirm", {"facility": "remote-facility"})
        self.callback("on_confirm", txn_id, {"contract": {"id": "c-1", "status": {"code": "ACTIVE"}}})
        self.assertFalse(ResourceRequest.objects.exists())
        message = BecknMessage.objects.get(direction=Direction.IN, action="on_confirm")
        self.assertEqual(message.processing_status, "failed")

    def test_error_callbacks_create_nothing(self):
        txn_id = self.new_transaction()
        self.bap(txn_id, "confirm", {"facility": str(self.origin.external_id)})
        self.callback("on_confirm", txn_id, {"contract": {"status": {"code": "ACTIVE"}}}, error={"code": "X"})
        self.assertFalse(ResourceRequest.objects.exists())

    def test_unknown_transaction_is_nacked(self):
        response = self.callback("on_confirm", "nope", {"contract": {"id": "c"}})
        self.assertEqual(response.json()["message"]["ack"]["status"], "NACK")

    def test_a_completed_callback_completes_the_origin_request(self):
        txn_id = self.new_transaction()
        self.bap(txn_id, "confirm", {"facility": str(self.origin.external_id)})
        self.callback("on_confirm", txn_id, {"contract": {"id": "c-1", "status": {"code": "ACTIVE"}}})
        message = {"contract": {"status": {"code": "COMPLETED"}, "contractAttributes": {"lifecycleState": "FULFILLED"}}}
        self.callback("on_update", txn_id, message, message_id="cb-3")
        self.assertEqual(ResourceRequest.objects.get().status, "completed")
