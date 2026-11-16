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
        self.assertEqual(Referral.objects.get().status, "pending")

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
