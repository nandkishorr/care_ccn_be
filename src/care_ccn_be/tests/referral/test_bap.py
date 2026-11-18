import uuid

from care.emr.models import Encounter
from care.emr.models.referral import Referral, ReferralQuestionnaireResponse
from django.urls import Resolver404, resolve
from model_bakery import baker

from care_ccn_be.models import BecknMessage, Direction
from care_ccn_be.models.referral import ReferralLink
from care_ccn_be.services import availability as availabilities
from care_ccn_be.services import referral as lifecycle
from care_ccn_be.tests.referral.base import ReferralTestBase

ROOT_URLCONF = "care_ccn_be.tests.referral.urls"
REMOTE_AVAILABILITY = "6f1c2b8e-1d0a-4c55-9a51-6d7bde3a2a10"
REMOTE_FACILITY = "0b6f0d55-5a54-4b8f-9d1e-2b7e1e3b9c01"
QUESTION = "8d3c8a51-2c3e-4f7a-b5f4-0f8b5b7f2c11"
REQUEST_FORM = {
    "id": "2a5d1f4e-7b1c-4d2a-9e6f-3c8b7a6d5e41",
    "slug": "referral-request-form",
    "version": "0.1",
    "title": "Referral Request Form",
    "status": "active",
    "subject_type": "referral",
    "internal_revision": 3,
    "questions": [{"id": QUESTION, "text": "Emergency Care", "type": "string", "link_id": "Q_af640a89"}],
}
RESPONSE_FORM = {
    **REQUEST_FORM,
    "id": "9c4e2b1a-5d6f-4a3b-8c7d-1e2f3a4b5c62",
    "slug": "referral-response-form1",
    "title": "Referral Response Form",
    "questions": [{"id": str(uuid.uuid5(uuid.NAMESPACE_URL, "r")), "text": "Emergency Response", "type": "string"}],
}
ANSWERS = {"results": [{"question_id": QUESTION, "values": [{"value": "Oxygen given"}]}]}


def on_select_message():
    return {
        "contract": {
            "status": {"code": "DRAFT"},
            "contractAttributes": {
                "@type": "hrf:HealthReferral",
                "referral": {
                    "availability": {
                        "id": REMOTE_AVAILABILITY,
                        "name": "Cardiology desk",
                        "referralType": "consultation",
                        "facilityId": REMOTE_FACILITY,
                        "facilityName": "Medicity",
                        "questionnaires": [
                            {"questionnaireType": "request", "questionnaire": REQUEST_FORM},
                            {"questionnaireType": "response", "questionnaire": RESPONSE_FORM},
                        ],
                    }
                },
            },
        }
    }


class AvailabilitySelectionTests(ReferralTestBase):
    """Care as the referring BAP: the BPP's availability and forms drive init / confirm."""

    def setUp(self):
        super().setUp()
        self.patient = self.create_patient(name="Ravi", gender="male")
        self.txn_id = self.new_transaction()
        self.bap(self.txn_id, "select", {"offer_id": f"offer-{REMOTE_AVAILABILITY}"})
        self.callback("on_select", self.txn_id, on_select_message())

    def sent_payload(self, action):
        return next(p for _, a, p in reversed(self.sent) if a == action)

    def body(self, **extra):
        return {"facility": str(self.origin.external_id), "patient": str(self.patient.external_id), **extra}

    def test_on_select_keeps_the_availability_and_its_forms_for_the_fe(self):
        availability = self.poll(self.txn_id)["availability"]
        self.assertEqual(
            (availability["id"], availability["referral_type"], availability["facility_id"]),
            (REMOTE_AVAILABILITY, "consultation", REMOTE_FACILITY),
        )
        forms = [
            (item["questionnaire_type"], item["questionnaire"]["title"]) for item in availability["questionnaires"]
        ]
        self.assertEqual(forms, [("request", "Referral Request Form"), ("response", "Referral Response Form")])

    def test_a_new_select_forgets_the_previous_availability(self):
        self.bap(self.txn_id, "select", {"offer_id": "offer-other"})
        self.assertIsNone(self.poll(self.txn_id)["availability"])

    def test_init_without_the_request_answers_is_a_400(self):
        response = self.bap(self.txn_id, "init", self.body())
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["errors"][0]["code"], "70006")

    def test_init_with_a_form_of_another_availability_is_a_400(self):
        body = self.body(questionnaire=RESPONSE_FORM["id"], questionnaire_response=ANSWERS)
        response = self.bap(self.txn_id, "init", body)
        self.assertEqual((response.status_code, response.json()["errors"][0]["code"]), (400, "70007"))
        malformed = self.body(questionnaire=REQUEST_FORM["id"], questionnaire_response={"results": "x"})
        self.assertEqual(self.bap(self.txn_id, "init", malformed).json()["errors"][0]["code"], "70007")

    def test_init_carries_the_availability_and_the_answered_request_form(self):
        body = self.body(questionnaire=REQUEST_FORM["id"], questionnaire_response=ANSWERS)
        self.assertEqual(self.bap(self.txn_id, "init", body).status_code, 202)
        attributes = self.sent_payload("init")["message"]["contract"]["contractAttributes"]
        self.assertEqual(attributes["assignedFacilityId"], REMOTE_FACILITY)
        self.assertEqual(attributes["targetCriteria"]["serviceCategory"]["code"], "CONSULTATION")
        referral = attributes["referral"]
        self.assertEqual(referral["availability"], {"id": REMOTE_AVAILABILITY})
        (form,) = referral["responses"]
        self.assertEqual(
            (form["ref"], form["questionnaireType"], form["status"], form["revision"]),
            (f"request-{self.txn_id}", "request", "completed", 3),
        )
        self.assertEqual((form["questionnaire"], form["questionnaireResponse"]), (REQUEST_FORM, ANSWERS))

    def test_an_on_init_error_creates_nothing_and_init_can_be_retried(self):
        body = self.body(questionnaire=REQUEST_FORM["id"], questionnaire_response=ANSWERS)
        self.bap(self.txn_id, "init", body)
        error = {"type": "DOMAIN-ERROR", "code": "70007", "message": "bad", "paths": [QUESTION]}
        self.callback("on_init", self.txn_id, {"contract": {"status": {"code": "DRAFT"}}}, error=error)
        poll = self.poll(self.txn_id)
        self.assertEqual(
            (poll["status"], poll["last_error"]["code"], poll["last_error"]["paths"]), ("ERROR", "70007", [QUESTION])
        )
        self.assertFalse(Referral.objects.exists())
        self.assertEqual(self.bap(self.txn_id, "init", body).status_code, 202)

    def test_on_confirm_creates_the_origin_referral_on_the_remote_availability_with_its_forms(self):
        body = self.body(questionnaire=REQUEST_FORM["id"], questionnaire_response=ANSWERS, title="Heart")
        self.bap(self.txn_id, "confirm", body)
        message = {"contract": {"id": "c-1", "status": {"code": "ACTIVE"}}}
        self.callback("on_confirm", self.txn_id, message)
        self.callback("on_confirm", self.txn_id, message, message_id="cb-2")
        referral = Referral.objects.get()
        self.assertEqual((referral.status, referral.title), ("in_progress", "Heart"))
        availability = referral.availability
        self.assertEqual(availability.name, "Cardiology desk - Medicity")
        self.assertEqual(
            availabilities.target_of(availability),
            {
                "bppId": "bpp.remote",
                "bppUri": "http://bpp.remote",
                "assignedFacilityId": REMOTE_FACILITY,
                "availabilityId": REMOTE_AVAILABILITY,
            },
        )
        mappings = availability.questionnaire_mappings.select_related("questionnaire").order_by("questionnaire_type")
        self.assertEqual(
            [(m.questionnaire_type, m.questionnaire.title, m.questionnaire.status) for m in mappings],
            [("request", "Referral Request Form", "retired"), ("response", "Referral Response Form", "retired")],
        )
        response = ReferralQuestionnaireResponse.objects.get(referral=referral)
        self.assertEqual(
            (response.questionnaire_type, response.questionnaire_snapshot, response.questionnaire_response),
            ("request", REQUEST_FORM, ANSWERS),
        )
        self.assertEqual((response.meta, response.questionnaire_revision), ({"ccn_ref": f"request-{self.txn_id}"}, 3))
        self.assertEqual(response.questionnaire, mappings[0].questionnaire)
        self.assertEqual([a for _, a, _ in self.sent].count("update"), 0)


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

    def test_discover_searches_manual_review_providers_by_text(self):
        self.stub_delivery()
        self.bap(body={"text_search": "Care Coordination Desk", "health_service_type": "CONSULTATION"})
        intent = self.sent_payload("discover")["message"]["intent"]
        self.assertEqual(
            intent,
            {
                "textSearch": "Care Coordination Desk",
                "filters": {
                    "type": "jsonpath",
                    "expression": '$.catalogs[*].resources[*] ? (@.resourceAttributes.acceptanceMode == "MANUAL_REVIEW")',
                },
            },
        )

    def test_discover_keeps_an_explicit_filter(self):
        self.stub_delivery()
        filters = {"type": "jsonpath", "expression": "$.catalogs[*]"}
        self.bap(body={"filters": filters})
        self.assertEqual(self.sent_payload("discover")["message"]["intent"], {"filters": filters})

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

    def test_on_init_only_validates_and_creates_no_referral(self):
        txn_id = self.new_transaction()
        patient = self.create_patient(name="Ravi")
        body = {"facility": str(self.origin.external_id), "patient": str(patient.external_id), "title": "Heart"}
        self.bap(txn_id, "init", body)
        self.callback("on_init", txn_id, {"contract": {"id": "c-1", "status": {"code": "DRAFT"}}})
        self.assertFalse(Referral.objects.exists())
        self.assertIsNone(self.poll(txn_id)["referral_id"])

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

    def test_on_confirm_after_init_creates_the_in_progress_referral(self):
        txn_id = self.new_transaction()
        body = {"facility": str(self.origin.external_id), "patient_name": "Asha"}
        self.bap(txn_id, "init", body)
        self.callback("on_init", txn_id, {"contract": {"id": "c-1", "status": {"code": "DRAFT"}}})
        self.assertFalse(Referral.objects.exists())
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

    def test_the_confirm_encounter_wins_over_an_init_without_one(self):
        txn_id = self.new_transaction()
        self.bap(txn_id, "init", {"facility": str(self.origin.external_id), "patient": str(self.patient.external_id)})
        self.callback("on_init", txn_id, {"contract": {"id": "c-1", "status": {"code": "DRAFT"}}})
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
