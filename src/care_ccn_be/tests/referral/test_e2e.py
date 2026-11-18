from care.emr.models import Encounter
from care.emr.models.referral import Referral
from care.emr.models.resource_request import ResourceRequest
from model_bakery import baker

from care_ccn_be.events import booking_fulfilled
from care_ccn_be.models import Adapter, BecknMessage, BecknTransaction, Delivery
from care_ccn_be.services import referral as lifecycle
from care_ccn_be.tests.referral.base import ReferralTestBase


class ReferralLoopbackTests(ReferralTestBase):
    """Care is the BAP, the network and the coordinating centre at once."""

    def setUp(self):
        super().setUp()
        self.receiving_availability(self.target, "lab")

    def test_select_forms_then_init_errors_then_confirm_and_the_response_comes_back(self):
        from care.emr.models.referral import ReferralQuestionnaireResponse

        from care_ccn_be.services import availability as availabilities

        request_form = self.referral_questionnaire()
        response_form = self.referral_questionnaire("Referral Response Form", "Emergency Response")
        self.map_questionnaire(self.receiving, request_form, "request")
        self.map_questionnaire(self.receiving, response_form, "response")
        self.loopback(nack_actions=("discover",))
        txn_id = self.bap().json()["transaction_id"]
        self.on_discover(txn_id, bppId="bpp.care", bppUri="http://bpp.care")
        patient = self.create_patient(name="Ravi", gender="male")

        self.bap(txn_id, "select", {"offer_id": f"offer-{self.receiving.external_id}"})
        availability = self.poll(txn_id)["availability"]
        self.assertEqual(
            [item["questionnaire"]["id"] for item in availability["questionnaires"]],
            [str(request_form.external_id), str(response_form.external_id)],
        )

        body = {"facility": str(self.origin.external_id), "patient": str(patient.external_id), "title": "Heart"}
        self.assertEqual(self.bap(txn_id, "init", body).status_code, 400)
        wrong = {"results": [{"question_id": str(response_form.questions[0]["id"]), "values": [{"value": "x"}]}]}
        self.bap(
            txn_id, "init", {**body, "questionnaire": str(request_form.external_id), "questionnaire_response": wrong}
        )
        poll = self.poll(txn_id)
        self.assertEqual((poll["status"], poll["last_error"]["code"]), ("ERROR", "70007"))

        answered = {
            **body,
            "questionnaire": str(request_form.external_id),
            "questionnaire_response": self.answers(request_form),
        }
        self.bap(txn_id, "init", answered)
        self.assertEqual(self.bap_txn(txn_id).status, "ON_INIT")
        self.assertFalse(Referral.objects.exists())

        self.bap(txn_id, "confirm", answered)
        self.assertEqual(self.bap_txn(txn_id).status, "ON_CONFIRM")
        origin = lifecycle.current(txn_id, "origin")
        assigned = lifecycle.current(txn_id, "assigned")
        self.assertEqual(assigned.availability, self.receiving)
        sent = ReferralQuestionnaireResponse.objects.get(referral=assigned)
        self.assertEqual((sent.questionnaire, sent.questionnaire_type), (request_form, "request"))
        kept = ReferralQuestionnaireResponse.objects.get(referral=origin)
        self.assertEqual((kept.questionnaire_type, kept.meta), ("request", {"ccn_ref": f"request-{txn_id}"}))

        ReferralQuestionnaireResponse.objects.create(
            referral=assigned,
            questionnaire=response_form,
            questionnaire_response=self.answers(response_form, "Admitted to ICU"),
            questionnaire_snapshot=availabilities.snapshot(response_form),
            questionnaire_type="response",
            created_by=self.user,
        )
        returned = ReferralQuestionnaireResponse.objects.get(referral=origin, questionnaire_type="response")
        self.assertEqual(returned.questionnaire_snapshot["title"], "Referral Response Form")
        self.assertEqual(returned.questionnaire_response["results"][0]["values"], [{"value": "Admitted to ICU"}])
        self.assertEqual(ReferralQuestionnaireResponse.objects.filter(referral=assigned).count(), 2)
        self.assertFalse(BecknMessage.objects.filter(processing_status="failed", action="on_update").exists())

    def test_discover_select_init_confirm_status(self):
        self.loopback(nack_actions=("discover",))
        txn_id = self.bap().json()["transaction_id"]
        self.on_discover(txn_id, bppId="cc.bpp", bppUri="http://cc.bpp")
        patient = self.create_patient(name="Ravi", gender="male")
        origin, target = str(self.origin.external_id), str(self.target.external_id)

        self.bap(txn_id, "select", {"offer_id": "offer-1"})
        self.assertEqual(self.bap_txn(txn_id).status, "ON_SELECT")

        init = {
            "facility": origin,
            "assigned_facility_id": target,
            "patient": str(patient.external_id),
            "title": "Heart",
        }
        self.bap(txn_id, "init", init)
        self.assertEqual(self.bap_txn(txn_id).status, "ON_INIT")
        self.assertFalse(Referral.objects.exists())

        self.bap(txn_id, "confirm", init)
        self.assertEqual((self.bap_txn(txn_id).status, self.bpp_txn(txn_id).status), ("ON_CONFIRM", "ON_CONFIRM"))
        origin_referral = lifecycle.current(txn_id, "origin")
        assigned_referral = lifecycle.current(txn_id, "assigned")
        self.assertEqual((origin_referral.status, assigned_referral.status), ("in_progress", "in_progress"))
        self.assertNotEqual(origin_referral.pk, assigned_referral.pk)
        self.assertEqual(Referral.objects.count(), 2)
        self.assertFalse(ResourceRequest.objects.exists())
        self.assertEqual(assigned_referral.patient.name, "Ravi")
        self.assertEqual(assigned_referral.availability.facility, self.target)

        self.bap(txn_id, "status")
        self.assertEqual(self.bap_txn(txn_id).status, "ON_STATUS")
        status = self.message(txn_id, "on_status")["message"]["contract"]
        self.assertEqual(status["status"]["code"], "ACTIVE")
        self.assertEqual(status["contractAttributes"]["referral"]["status"], "in_progress")

        poll = self.poll(txn_id)
        self.assertEqual(poll["referral_id"], str(origin_referral.external_id))
        self.assertEqual(poll["contract_id"], str(assigned_referral.external_id))
        self.assertEqual(self.poll(str(origin_referral.external_id))["transaction_id"], txn_id)
        self.assertEqual(
            poll["actions"],
            [
                "DISCOVER",
                "ON_DISCOVER",
                "SELECT",
                "ON_SELECT",
                "INIT",
                "ON_INIT",
                "CONFIRM",
                "ON_CONFIRM",
                "STATUS",
                "ON_STATUS",
            ],
        )
        self.assertFalse(BecknMessage.objects.filter(delivery_status=Delivery.FAILED).exists())
        self.assertFalse(BecknMessage.objects.filter(processing_status="failed").exists())
        self.assertEqual(
            list(BecknMessage.objects.filter(delivery_status=Delivery.NACKED).values_list("action", flat=True)),
            ["discover"],
        )

    def test_a_fulfilled_booking_completes_both_referrals(self):
        self.loopback(nack_actions=("discover",))
        txn_id = self.bap().json()["transaction_id"]
        self.on_discover(txn_id, bppId="cc.bpp", bppUri="http://cc.bpp")
        body = {
            "facility": str(self.origin.external_id),
            "assigned_facility_id": str(self.target.external_id),
            "patient_name": "Asha",
        }
        self.bap(txn_id, "confirm", body)
        appointment = BecknTransaction.objects.create(
            transaction_id="appt-1", adapter=Adapter.BPP, flow="appointment", coordination_id=txn_id
        )
        booking_fulfilled.send(sender=None, transaction=appointment, booking=None)
        self.assertEqual(lifecycle.current(txn_id, "assigned").status, "completed")
        self.assertEqual(lifecycle.current(txn_id, "origin").status, "completed")
        self.assertEqual(Referral.objects.count(), 2)
        self.assertFalse(ResourceRequest.objects.exists())

    def test_a_referral_created_in_care_travels_the_whole_loop(self):
        self.loopback()
        patient = self.create_patient(name="Asha", gender="female")
        encounter = baker.make(Encounter, patient=patient, facility=self.origin)
        referral = self.create_referral(
            patient=patient, encounter=encounter, availability=self.network_availability(assigned=self.target)
        )
        from care_ccn_be.tasks.referral import submit_referral

        self.assertEqual(submit_referral(referral.pk), "sent")
        txn_id = str(referral.external_id)
        self.assertEqual((self.bap_txn(txn_id).status, self.bpp_txn(txn_id).status), ("ON_CONFIRM", "ON_CONFIRM"))
        referral.refresh_from_db()
        self.assertEqual(referral.status, "in_progress")
        self.assertEqual(lifecycle.current(txn_id, "origin").pk, referral.pk)
        assigned = lifecycle.current(txn_id, "assigned")
        self.assertEqual(assigned.availability.facility, self.target)
        self.assertEqual(assigned.patient.name, "Asha")
        self.assertEqual(Referral.objects.count(), 2)
        self.assertFalse(ResourceRequest.objects.exists())
        self.assertEqual(self.poll(txn_id)["referral_id"], txn_id)

    def test_an_api_referral_from_an_encounter_links_only_the_origin_referral(self):
        self.loopback(nack_actions=("discover",))
        txn_id = self.bap().json()["transaction_id"]
        self.on_discover(txn_id, bppId="cc.bpp", bppUri="http://cc.bpp")
        patient = self.create_patient(name="Ravi", gender="male")
        encounter = baker.make(Encounter, patient=patient, facility=self.origin)
        body = {
            "facility": str(self.origin.external_id),
            "assigned_facility_id": str(self.target.external_id),
            "encounter": str(encounter.external_id),
            "title": "Heart",
        }
        self.bap(txn_id, "init", body)
        self.bap(txn_id, "confirm", body)
        origin_referral = lifecycle.current(txn_id, "origin")
        assigned_referral = lifecycle.current(txn_id, "assigned")
        self.assertEqual((origin_referral.encounter, origin_referral.patient), (encounter, patient))
        self.assertEqual(origin_referral.status, "in_progress")
        self.assertIsNone(assigned_referral.encounter)
        self.assertEqual(Referral.objects.count(), 2)
