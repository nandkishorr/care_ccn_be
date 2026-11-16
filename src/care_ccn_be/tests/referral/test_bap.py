import uuid

from care.emr.models import Encounter
from care.emr.models.referral import Referral
from django.urls import Resolver404, resolve
from model_bakery import baker

from care_ccn_be.models import BecknMessage, Direction
from care_ccn_be.models.referral import ReferralLink
from care_ccn_be.services import referral as lifecycle
from care_ccn_be.tests.referral.base import ReferralTestBase

ROOT_URLCONF = "care_ccn_be.tests.referral.urls"


class FlowAdapterTests(ReferralTestBase):
    def sent_payload(self, action):
        return next(p for _, a, p in reversed(self.sent) if a == action)

    def test_discover_defaults_to_the_service_coordination_schema_without_a_bpp(self):
        self.stub_delivery()
        self.bap(body={"health_service_type": "CONSULTATION"})
        context = self.sent_payload("discover")["context"]
        self.assertEqual(
            context["schemaContext"], ["https://schema.beckn.io/ServiceCoordinationResource/v2.1/context.jsonld"]
        )
        self.assertNotIn("bppId", context)

    def test_select_requires_an_offer_id(self):
        txn_id = self.new_transaction()
        response = self.bap(txn_id, "select", {})
        self.assertEqual(response.status_code, 400)
        self.assertIn("offer_id is required", str(response.json()))

    def test_camel_case_bodies_are_rejected(self):
        txn_id = self.new_transaction()
        self.assertEqual(self.bap(txn_id, "select", {"offerId": "offer-1"}).status_code, 400)

    def test_select_payload_is_a_draft_with_the_offer(self):
        txn_id = self.new_transaction()
        self.assertEqual(self.bap(txn_id, "select", {"offer_id": "offer-1"}).status_code, 202)
        contract = self.sent_payload("select")["message"]["contract"]
        self.assertEqual(contract["status"]["code"], "DRAFT")
        self.assertEqual(contract["commitments"][0]["offer"]["id"], "offer-1")
        self.assertEqual(contract["commitments"][0]["id"], f"commitment-{txn_id}")
        self.assertEqual(contract["contractAttributes"]["coordinationId"], txn_id)
        self.assertEqual(self.poll(txn_id)["actions"][-1], "SELECT")

    def test_confirm_carries_the_patient_name_participant_and_stores_params(self):
        txn_id = self.new_transaction()
        self.bap(txn_id, "select", {"offer_id": "offer-1"})
        body = {"facility": str(self.origin.external_id), "patient_name": "Asha", "title": "Heart", "reason": "pain"}
        self.assertEqual(self.bap(txn_id, "confirm", body).status_code, 202)
        contract = self.sent_payload("confirm")["message"]["contract"]
        self.assertEqual(contract["status"]["code"], "ACTIVE")
        self.assertEqual(contract["descriptor"], {"name": "Heart", "shortDesc": "pain"})
        self.assertEqual(contract["commitments"][0]["offer"]["id"], "offer-1")
        (participant,) = contract["participants"]
        self.assertEqual(participant["descriptor"]["name"], "Asha")
        self.assertEqual(participant["id"], f"participant-patient-{txn_id}")
        self.assertEqual(contract["contractAttributes"]["facilityId"], str(self.origin.external_id))
        self.assertEqual(self.poll(txn_id)["confirm"]["patient_name"], "Asha")

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
        body = {"facility": str(self.origin.external_id), "patient": str(uuid.uuid4())}
        self.assertEqual(self.bap(txn_id, "confirm", body).status_code, 404)

    def test_routing_overrides_pick_the_bpp(self):
        txn_id = self.new_transaction()
        context = {
            "network_id": "nfh-test",
            "bap_id": "care-bap",
            "bap_uri": "http://care-bap",
            "bpp_id": "bpp.x",
            "bpp_uri": "http://bpp.x",
        }
        self.assertEqual(self.bap(txn_id, "select", {"offer_id": "o", "context": context}).status_code, 202)
        context = self.sent_payload("select")["context"]
        self.assertEqual((context["bppId"], context["bppUri"]), ("bpp.x", "http://bpp.x"))

    def test_the_default_bpp_is_the_coordination_centre(self):
        txn_id = self.new_transaction()
        self.bap(txn_id, "select", {"offer_id": "o"})
        self.assertEqual(self.sent_payload("select")["context"]["bppId"], "cc.bpp")

    def test_passthrough_select_needs_an_offer_and_confirm_a_facility(self):
        txn_id = self.new_transaction()
        self.assertEqual(self.bap(txn_id, "select", {"message": {"contract": {"id": "c"}}}).status_code, 400)
        self.assertEqual(self.bap(txn_id, "confirm", {"message": {"contract": {"id": "c"}}}).status_code, 400)
        select = {"contract": {"commitments": [{"offer": {"id": "o"}}]}}
        self.assertEqual(self.bap(txn_id, "select", {"message": select}).status_code, 202)
        confirm = {"contract": {"contractAttributes": {"facilityId": str(self.origin.external_id)}}}
        self.assertEqual(self.bap(txn_id, "confirm", {"message": confirm}).status_code, 202)
        self.assertEqual(self.poll(txn_id)["confirm"]["facility"], str(self.origin.external_id))

    def test_unknown_spec_field_is_a_400(self):
        txn_id = self.new_transaction()
        self.assertEqual(self.bap(txn_id, "init", {"facility": "f", "bogus": 1}).status_code, 400)

    def test_cancel_is_not_exposed(self):
        with self.assertRaises(Resolver404):
            resolve("/bap/referral/txn-1/cancel/", urlconf=ROOT_URLCONF)

    def test_status_sends_the_contract_id(self):
        txn_id = self.new_transaction()
        self.assertEqual(self.bap(txn_id, "status").status_code, 202)
        self.assertEqual(self.sent_payload("status")["message"]["contract"]["id"], txn_id)


class BAPReceiverTests(ReferralTestBase):
    def test_on_discover_records_routing_and_catalogs(self):
        txn_id = self.new_transaction()
        self.assertEqual(self.message(txn_id, "on_discover")["message"]["catalogs"][0]["id"], "c")
        catalogs = self.client.get(f"/bap/referral/{txn_id}/catalogs/").json()["catalogs"]
        self.assertEqual([catalog["id"] for catalog in catalogs], ["c"])

    def test_on_init_stores_the_payload_and_the_poll_shows_it(self):
        txn_id = self.new_transaction()
        self.bap(txn_id, "init", {"facility": str(self.origin.external_id)})
        message = {"contract": {"id": "c-1", "status": {"code": "DRAFT"}}}
        self.assertEqual(self.callback("on_init", txn_id, message).json()["message"]["ack"]["status"], "ACK")
        self.assertEqual(self.message(txn_id, "on_init")["message"], message)
        poll = self.poll(txn_id)
        self.assertEqual(poll["contract_id"], "c-1")
        self.assertEqual(poll["routing"]["bpp_id"], "bpp.remote")

    def test_on_init_creates_the_pending_origin_referral(self):
        txn_id = self.new_transaction()
        patient = self.create_patient(name="Ravi")
        body = {"facility": str(self.origin.external_id), "patient": str(patient.external_id), "title": "Heart"}
        self.bap(txn_id, "init", body)
        self.callback("on_init", txn_id, {"contract": {"id": "c-1", "status": {"code": "DRAFT"}}})
        referral = lifecycle.current(txn_id, lifecycle.ORIGIN)
        self.assertEqual((referral.status, referral.title, referral.patient), ("pending", "Heart", patient))
        self.assertEqual(self.poll(txn_id)["referral_id"], str(referral.external_id))

    def test_on_confirm_creates_the_in_progress_origin_referral_from_the_stored_params(self):
        txn_id = self.new_transaction()
        patient = self.create_patient(name="Ravi")
        body = {
            "facility": str(self.origin.external_id),
            "patient": str(patient.external_id),
            "title": "Heart",
            "reason": "pain",
            "category": "patient_care",
            "emergency": True,
            "assigned_facility_id": "remote-facility",
        }
        self.bap(txn_id, "confirm", body)
        message = {"contract": {"id": "c-1", "status": {"code": "ACTIVE"}}}
        self.callback("on_confirm", txn_id, message)
        referral = Referral.objects.get()
        self.assertEqual(referral.status, "in_progress")
        self.assertEqual((referral.title, referral.note, referral.patient), ("Heart", "pain", patient))
        availability = referral.availability
        self.assertEqual((availability.entity_type, availability.referral_type), ("external", "consultation"))
        self.assertEqual(
            availability.external_entity["beckn"],
            {"bppId": "bpp.remote", "bppUri": "http://bpp.remote", "assignedFacilityId": "remote-facility"},
        )
        self.assertEqual(referral.created_by, self.system)
        link = ReferralLink.objects.get()
        self.assertEqual(
            link.details, {"urgency": "EMERGENCY", "originFacilityId": str(self.origin.external_id), "confirmed": True}
        )

    def test_referral_id_resolves_the_original_transaction_and_status_action(self):
        txn_id = self.new_transaction()
        body = {"facility": str(self.origin.external_id), "patient_name": "Asha"}
        self.bap(txn_id, "confirm", body)
        self.callback("on_confirm", txn_id, {"contract": {"id": "c-1", "status": {"code": "ACTIVE"}}})
        referral_id = str(Referral.objects.get().external_id)
        self.assertNotEqual(referral_id, txn_id)
        self.assertEqual(self.poll(referral_id)["transaction_id"], txn_id)
        self.assertEqual(self.bap(referral_id, "status").status_code, 202)
        payload = next(payload for _, action, payload in reversed(self.sent) if action == "status")
        self.assertEqual(payload["context"]["transactionId"], txn_id)

    def test_on_confirm_stores_remote_referral_id_for_a_new_origin(self):
        txn_id = self.new_transaction()
        self.bap(txn_id, "confirm", {"facility": str(self.origin.external_id), "patient_name": "Asha"})
        message = {
            "contract": {
                "id": "contract-1",
                "status": {"code": "ACTIVE"},
                "contractAttributes": {"referral": {"id": "remote-referral-1"}},
            }
        }
        self.callback("on_confirm", txn_id, message)
        link = ReferralLink.objects.get()
        self.assertEqual(link.details["external_referral_id"], "remote-referral-1")
        self.assertEqual(self.poll(txn_id)["contract_id"], "contract-1")
        self.callback(
            "on_confirm",
            txn_id,
            {"contract": {"id": "contract-1", "status": {"code": "ACTIVE"}}},
            message_id="cb-2",
        )
        link.refresh_from_db()
        self.assertEqual(link.details["external_referral_id"], "remote-referral-1")

    def test_a_non_consultation_category_is_a_lab_referral(self):
        txn_id = self.new_transaction()
        body = {"facility": str(self.origin.external_id), "patient_name": "Asha", "category": "medicines"}
        self.bap(txn_id, "confirm", body)
        self.callback("on_confirm", txn_id, {"contract": {"id": "c-1", "status": {"code": "ACTIVE"}}})
        self.assertEqual(Referral.objects.get().availability.referral_type, "lab")

    def test_a_patient_name_creates_the_patient_of_the_origin_referral(self):
        txn_id = self.new_transaction()
        self.bap(txn_id, "confirm", {"facility": str(self.origin.external_id), "patient_name": "Asha Rao"})
        self.callback("on_confirm", txn_id, {"contract": {"id": "c-1", "status": {"code": "ACTIVE"}}})
        self.assertEqual(Referral.objects.get().patient.name, "Asha Rao")

    def test_repeated_on_confirm_never_creates_a_second_origin_referral(self):
        txn_id = self.new_transaction()
        self.bap(txn_id, "confirm", {"facility": str(self.origin.external_id), "patient_name": "Asha"})
        message = {"contract": {"id": "c-1", "status": {"code": "ACTIVE"}}}
        self.callback("on_confirm", txn_id, message, message_id="cb-1")
        self.callback("on_confirm", txn_id, message, message_id="cb-2")
        self.assertEqual(Referral.objects.count(), 1)

    def test_on_confirm_approves_the_existing_referral(self):
        txn_id = self.new_transaction()
        body = {"facility": str(self.origin.external_id), "patient_name": "Asha"}
        self.bap(txn_id, "init", body)
        self.callback("on_init", txn_id, {"contract": {"id": "c-1", "status": {"code": "DRAFT"}}})
        self.assertEqual(Referral.objects.get().status, "pending")
        self.bap(txn_id, "confirm", body)
        self.callback("on_confirm", txn_id, {"contract": {"id": "c-1", "status": {"code": "ACTIVE"}}})
        self.assertEqual(Referral.objects.get().status, "in_progress")

    def test_on_confirm_without_a_resolvable_facility_marks_the_message_failed(self):
        txn_id = self.new_transaction()
        unknown_facility = str(uuid.uuid4())
        self.assertEqual(
            self.bap(txn_id, "confirm", {"facility": unknown_facility, "patient_name": "Asha"}).status_code, 202
        )
        self.callback("on_confirm", txn_id, {"contract": {"id": "c-1", "status": {"code": "ACTIVE"}}})
        self.assertFalse(Referral.objects.exists())
        message = BecknMessage.objects.get(direction=Direction.IN, action="on_confirm")
        self.assertEqual(message.processing_status, "failed")

    def test_on_confirm_without_a_patient_marks_the_message_failed(self):
        txn_id = self.new_transaction()
        self.bap(txn_id, "confirm", {"facility": str(self.origin.external_id)})
        self.callback("on_confirm", txn_id, {"contract": {"id": "c-1", "status": {"code": "ACTIVE"}}})
        self.assertFalse(Referral.objects.exists())
        message = BecknMessage.objects.get(direction=Direction.IN, action="on_confirm")
        self.assertEqual(message.processing_status, "failed")

    def test_passthrough_confirm_keeps_the_patient_for_the_origin_referral(self):
        txn_id = self.new_transaction()
        confirm = self.referral_message(facility=self.origin)
        self.bap(txn_id, "confirm", {"message": confirm})
        self.callback("on_confirm", txn_id, {"contract": {"id": "c-1", "status": {"code": "ACTIVE"}}})
        self.assertEqual(Referral.objects.get().patient.name, "Asha Rao")

    def test_error_callbacks_create_nothing(self):
        txn_id = self.new_transaction()
        self.bap(txn_id, "confirm", {"facility": str(self.origin.external_id), "patient_name": "Asha"})
        self.callback("on_confirm", txn_id, {"contract": {"status": {"code": "ACTIVE"}}}, error={"code": "X"})
        self.assertFalse(Referral.objects.exists())

    def test_unknown_transaction_is_nacked(self):
        response = self.callback("on_confirm", "nope", {"contract": {"id": "c"}})
        self.assertEqual(response.json()["message"]["ack"]["status"], "NACK")

    def test_a_completed_callback_completes_the_origin_referral(self):
        txn_id = self.new_transaction()
        self.bap(txn_id, "confirm", {"facility": str(self.origin.external_id), "patient_name": "Asha"})
        self.callback("on_confirm", txn_id, {"contract": {"id": "c-1", "status": {"code": "ACTIVE"}}})
        message = {"contract": {"status": {"code": "COMPLETED"}, "contractAttributes": {"lifecycleState": "FULFILLED"}}}
        self.callback("on_update", txn_id, message, message_id="cb-3")
        self.assertEqual(Referral.objects.get().status, "completed")


class EncounterReferralTests(ReferralTestBase):
    def setUp(self):
        super().setUp()
        self.patient = self.create_patient(name="Ravi", gender="male")
        self.encounter = baker.make(Encounter, patient=self.patient, facility=self.origin)

    def sent_payload(self, action):
        return next(p for _, a, p in reversed(self.sent) if a == action)

    def body(self, **extra):
        return {"facility": str(self.origin.external_id), "encounter": str(self.encounter.external_id), **extra}

    def test_on_confirm_links_the_origin_referral_to_the_encounter_and_its_patient(self):
        txn_id = self.new_transaction()
        self.assertEqual(self.bap(txn_id, "confirm", self.body()).status_code, 202)
        (participant,) = self.sent_payload("confirm")["message"]["contract"]["participants"]
        self.assertEqual(participant["id"], f"participant-patient-{self.patient.external_id}")
        self.assertNotIn(str(self.encounter.external_id), str(self.sent_payload("confirm")))
        self.callback("on_confirm", txn_id, {"contract": {"id": "c-1", "status": {"code": "ACTIVE"}}})
        referral = lifecycle.current(txn_id, lifecycle.ORIGIN)
        self.assertEqual((referral.encounter, referral.patient), (self.encounter, self.patient))

    def test_on_init_links_the_pending_origin_referral_to_the_encounter(self):
        txn_id = self.new_transaction()
        self.bap(txn_id, "init", self.body(patient=str(self.patient.external_id)))
        self.callback("on_init", txn_id, {"contract": {"id": "c-1", "status": {"code": "DRAFT"}}})
        referral = Referral.objects.get()
        self.assertEqual((referral.status, referral.encounter), ("pending", self.encounter))

    def test_on_confirm_links_an_existing_origin_referral_to_the_encounter(self):
        txn_id = self.new_transaction()
        self.bap(txn_id, "init", {"facility": str(self.origin.external_id), "patient": str(self.patient.external_id)})
        self.callback("on_init", txn_id, {"contract": {"id": "c-1", "status": {"code": "DRAFT"}}})
        self.assertIsNone(Referral.objects.get().encounter)
        self.bap(txn_id, "confirm", self.body())
        self.callback("on_confirm", txn_id, {"contract": {"id": "c-1", "status": {"code": "ACTIVE"}}})
        referral = Referral.objects.get()
        self.assertEqual((referral.status, referral.encounter), ("in_progress", self.encounter))

    def test_an_encounter_of_another_patient_is_never_linked(self):
        other = self.create_patient(name="Asha", gender="female")
        referral = self.create_referral(patient=other)
        lifecycle.link_encounter(referral, self.encounter)
        referral.refresh_from_db()
        self.assertIsNone(referral.encounter)

    def test_passthrough_confirm_links_the_encounter(self):
        txn_id = self.new_transaction()
        confirm = self.referral_message(facility=self.origin)
        self.bap(txn_id, "confirm", {"message": confirm, "encounter": str(self.encounter.external_id)})
        self.callback("on_confirm", txn_id, {"contract": {"id": "c-1", "status": {"code": "ACTIVE"}}})
        referral = Referral.objects.get()
        self.assertEqual((referral.encounter, referral.patient), (self.encounter, self.patient))

    def test_an_encounter_of_another_patient_is_a_400(self):
        txn_id = self.new_transaction()
        other = self.create_patient(name="Asha", gender="female")
        response = self.bap(txn_id, "confirm", self.body(patient=str(other.external_id)))
        self.assertEqual(response.status_code, 400)
        self.assertIn("Patient does not match the encounter patient", str(response.json()))

    def test_an_encounter_of_another_facility_is_a_400(self):
        txn_id = self.new_transaction()
        response = self.bap(txn_id, "init", self.body(facility=str(self.target.external_id)))
        self.assertEqual(response.status_code, 400)
        self.assertIn("Facility does not match the encounter facility", str(response.json()))

    def test_an_unknown_encounter_is_a_404(self):
        txn_id = self.new_transaction()
        self.assertEqual(self.bap(txn_id, "confirm", self.body(encounter=str(uuid.uuid4()))).status_code, 404)
