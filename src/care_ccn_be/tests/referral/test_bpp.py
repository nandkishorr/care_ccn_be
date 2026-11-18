import uuid

from care.emr.models import Patient
from care.emr.models.patient import PatientIdentifier
from care.emr.models.referral import Referral, ReferralQuestionnaireResponse

from care_ccn_be.models.referral import ReferralLink
from care_ccn_be.services import availability as availabilities
from care_ccn_be.services import referral as lifecycle
from care_ccn_be.tests.referral.base import ReferralTestBase


class ReceivingAvailabilityTests(ReferralTestBase):
    """Care as the receiving BPP: only a published availability receives, and its request form must be answered."""

    def setUp(self):
        super().setUp()
        self.stub_delivery()
        self.request_form = self.referral_questionnaire()
        self.response_form = self.referral_questionnaire("Referral Response Form", "Emergency Response")
        self.map_questionnaire(self.receiving, self.request_form, "request")
        self.map_questionnaire(self.receiving, self.response_form, "response")

    def callbacks(self, action):
        return [p for _, a, p in self.sent if a == action]

    def select(self, offer_id, txn="coord-1"):
        message = {
            "contract": {
                "commitments": [{"id": "commitment-1", "offer": {"id": offer_id}}],
                "contractAttributes": {"@type": "hrf:HealthReferral", "coordinationId": txn},
            }
        }
        self.request("select", txn, message)
        return self.callbacks("on_select")[-1]

    def contract(self, availability=None, answers=None, questionnaire=None, assigned=None):
        """A direct BAP -> BPP request: the referring facility lives in another instance."""
        availability = availability or self.receiving
        referral = {"availability": {"id": str(availability.external_id)}}
        if answers is not None:
            snapshot = availabilities.snapshot(questionnaire or self.request_form)
            referral["responses"] = [
                {
                    "ref": "request-coord-1",
                    "questionnaireType": "request",
                    "status": "completed",
                    "questionnaire": snapshot,
                    "questionnaireResponse": answers,
                }
            ]
        return self.referral_message(
            facilityId="remote-origin-facility",
            assignedFacilityId=str((assigned or self.target).external_id),
            referral=referral,
        )

    def error_of(self, action):
        return self.callbacks(action)[-1].get("error")

    def test_select_returns_the_availability_and_both_forms(self):
        callback = self.select(f"offer-{self.receiving.external_id}")
        self.assertNotIn("error", callback)
        block = callback["message"]["contract"]["contractAttributes"]["referral"]["availability"]
        self.assertEqual(
            (block["id"], block["name"], block["referralType"], block["facilityId"]),
            (str(self.receiving.external_id), "Consultation desk", "consultation", str(self.target.external_id)),
        )
        forms = {item["questionnaireType"]: item["questionnaire"] for item in block["questionnaires"]}
        self.assertEqual(set(forms), {"request", "response"})
        request = forms["request"]
        self.assertEqual(
            (request["id"], request["title"]), (str(self.request_form.external_id), "Referral Request Form")
        )
        self.assertEqual(request["questions"][0]["text"], "Emergency Care")
        self.assertNotIn("created_by", request)

    def test_select_of_an_unknown_or_private_availability_is_unavailable(self):
        self.assertEqual(self.select(f"offer-{uuid.uuid4()}")["error"]["code"], "70005")
        private = self.receiving_availability(self.target, "lab", is_public=False)
        self.assertEqual(self.select(f"offer-{private.external_id}", txn="coord-2")["error"]["code"], "70005")

    def test_init_without_the_request_form_is_rejected(self):
        self.request("init", "coord-1", self.contract())
        error = self.error_of("on_init")
        self.assertEqual(error["code"], "70006")
        self.assertIn("must be answered", error["message"])
        self.assertFalse(Referral.objects.exists())

    def test_init_with_invalid_answers_is_rejected_with_the_questions(self):
        answers = {"results": [{"question_id": str(uuid.uuid4()), "values": [{"value": "x"}]}]}
        self.request("init", "coord-1", self.contract(answers=answers))
        error = self.error_of("on_init")
        self.assertEqual(error["code"], "70007")
        self.assertFalse(Referral.objects.exists())

    def test_a_form_that_is_not_the_availability_request_form_is_rejected(self):
        answers = self.answers(self.response_form)
        self.request("init", "coord-1", self.contract(answers=answers, questionnaire=self.response_form))
        self.assertEqual(self.error_of("on_init")["code"], "70007")

    def test_an_availability_of_another_facility_is_rejected(self):
        self.request("init", "coord-1", self.contract(answers=self.answers(self.request_form), assigned=self.origin))
        error = self.error_of("on_init")
        self.assertEqual(error["code"], "70005")
        self.assertIn("does not accept network referrals", error["message"])

    def test_a_facility_without_an_availability_does_not_accept_network_referrals(self):
        message = self.referral_message(facilityId="remote-origin-facility", assigned=self.origin)
        self.request("init", "coord-1", message)
        self.assertEqual(self.error_of("on_init")["code"], "70005")
        self.request("confirm", "coord-1", message, message_id="m-2")
        self.assertEqual(self.error_of("on_confirm")["code"], "70005")
        self.assertFalse(Referral.objects.exists())

    def test_a_valid_init_creates_nothing_and_confirm_creates_the_assigned_referral_with_the_form(self):
        member = {"user_id": self.user.id, "role": {"code": "doctor"}}
        self.receiving.care_team = [member]
        self.receiving.save()
        message = self.contract(answers=self.answers(self.request_form))
        self.request("init", "coord-1", message)
        self.assertIsNone(self.error_of("on_init"))
        attributes = self.callbacks("on_init")[-1]["message"]["contract"]["contractAttributes"]
        self.assertEqual(attributes["referral"]["availability"]["id"], str(self.receiving.external_id))
        self.assertFalse(Referral.objects.exists())

        self.request("confirm", "coord-1", message, message_id="m-2")
        self.request("confirm", "coord-1", message, message_id="m-3")
        self.assertIsNone(self.error_of("on_confirm"))
        referral = Referral.objects.get()
        self.assertEqual((referral.availability, referral.status), (self.receiving, "in_progress"))
        self.assertEqual(referral.assignees, [member])
        self.assertEqual(lifecycle.current("coord-1", lifecycle.ASSIGNED), referral)
        response = ReferralQuestionnaireResponse.objects.get(referral=referral)
        self.assertEqual(
            (response.questionnaire, response.questionnaire_type, response.meta),
            (self.request_form, "request", {"ccn_ref": "request-coord-1"}),
        )
        self.assertEqual(response.questionnaire_response["results"][0]["values"][0]["value"], "Oxygen given")
        self.assertIn("cleaned_response", response.questionnaire_response)
        self.assertEqual([a for _, a, _ in self.sent], ["on_init", "on_confirm", "on_confirm"])

    def test_an_availability_without_a_request_form_needs_no_answers(self):
        open_desk = self.receiving_availability(self.target, "lab")
        self.request("confirm", "coord-1", self.contract(availability=open_desk))
        self.assertEqual(Referral.objects.get().availability, open_desk)
        self.assertFalse(ReferralQuestionnaireResponse.objects.exists())

    def test_a_revised_request_form_must_be_selected_again(self):
        revision = self.referral_questionnaire()
        self.request_form.latest_revision = revision
        self.request_form.save()
        self.request("init", "coord-1", self.contract(answers=self.answers(self.request_form)))
        self.assertEqual(self.error_of("on_init")["code"], "70007")


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

    def test_init_only_validates_and_creates_nothing(self):
        message = self.referral_message(facility=self.origin)
        self.assertEqual(self.ack(self.request("init", "coord-1", message)), "ACK")
        self.assertFalse(Referral.objects.exists())
        self.assertFalse(Patient.objects.filter(name="Asha Rao").exists())
        (callback,) = self.callbacks("on_init")
        self.assertNotIn("error", callback)
        contract = callback["message"]["contract"]
        self.assertEqual(contract["status"]["code"], "DRAFT")
        self.assertEqual(contract["contractAttributes"]["lifecycleState"], "DRAFT")

    def test_confirm_creates_the_origin_referral_and_patient(self):
        message = self.referral_message(facility=self.origin)
        self.assertEqual(self.ack(self.request("confirm", "coord-1", message)), "ACK")
        referral = Referral.objects.get()
        self.assertEqual(referral.status, "in_progress")
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
        (callback,) = self.callbacks("on_confirm")
        contract = callback["message"]["contract"]
        self.assertEqual(contract["status"]["code"], "ACTIVE")
        self.assertEqual(contract["id"], str(referral.external_id))
        self.assertEqual(contract["contractAttributes"]["referral"]["patient"]["name"], "Asha Rao")
        self.assertEqual(contract["contractAttributes"]["referral"]["priority"], 2)

    def test_confirm_for_an_assigned_only_facility_creates_the_assigned_referral(self):
        self.request("confirm", "coord-1", self.referral_message(assigned=self.target))
        referral = Referral.objects.get()
        self.assertEqual(referral.availability, self.receiving)
        self.assertEqual(ReferralLink.objects.get().role, "assigned")

    def test_a_lab_referral_lands_on_a_lab_availability(self):
        lab = self.receiving_availability(self.target, "lab")
        criteria = {"serviceCategory": {"code": "INVESTIGATION"}}
        message = self.referral_message(assigned=self.target, targetCriteria=criteria)
        self.request("confirm", "coord-1", message)
        self.assertEqual(Referral.objects.get().availability, lab)

    def test_the_assigned_referral_is_assigned_to_the_availability_care_team(self):
        member = {"user_id": self.user.id, "role": {"code": "doctor"}}
        self.receiving.care_team = [member]
        self.receiving.save()
        self.request("confirm", "coord-1", self.referral_message(assigned=self.target))
        referral = Referral.objects.get()
        self.assertEqual(referral.availability, self.receiving)
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
        self.request("confirm", "coord-1", message)
        self.assertEqual(Patient.objects.get(name="Asha Rao").gender, "non_binary")

    def test_an_existing_patient_is_matched_by_health_id(self):
        self.request("confirm", "coord-1", self.referral_message(facility=self.origin))
        self.request(
            "confirm",
            "coord-2",
            self.referral_message(coordination_id="coord-2", facility=self.origin),
            message_id="m-2",
        )
        self.assertEqual(Patient.objects.filter(name="Asha Rao").count(), 1)
        self.assertEqual(Referral.objects.count(), 2)

    def test_unsupported_actions_are_nacked(self):
        response = self.request("cancel", "coord-1", self.referral_message())
        self.assertEqual(self.ack(response), "NACK")

    def test_an_update_for_an_unknown_referral_answers_an_error(self):
        self.assertEqual(self.ack(self.request("update", "coord-1", self.referral_message())), "ACK")
        (callback,) = self.callbacks("on_update")
        self.assertIn("Referral not found", callback["error"]["message"])

    def test_inbound_referral_is_routed_to_the_flow_and_the_coordination_id_is_stored(self):
        self.request("select", "coord-7", self.referral_message(coordination_id="coord-7"))
        txn = self.bpp_txn_of("coord-7")
        self.assertEqual((txn.flow, txn.coordination_id), ("referral", "coord-7"))
