from care.emr.models import Encounter
from care.emr.models.referral import Referral
from model_bakery import baker

from care_ccn_be.events import booking_fulfilled
from care_ccn_be.flows.referral import lifecycle
from care_ccn_be.models import BecknMessage, BecknTransaction, Delivery, Side
from care_ccn_be.tests.referral.base import ReferralTestBase


class ReferralLoopbackTests(ReferralTestBase):
    """Care is the BAP, the network and the coordinating centre at once."""

    def test_discover_select_init_confirm_status(self):
        self.loopback(nack_actions=("discover",))
        txn_id = self.bap().json()["transactionId"]
        self.on_discover(txn_id, bppId="cc.bpp", bppUri="http://cc.bpp")
        patient = self.create_patient(name="Ravi", gender="male")
        origin, target = str(self.origin.external_id), str(self.target.external_id)

        self.bap(txn_id, "select", {"offerId": "offer-1"})
        self.assertEqual(self.bap_txn(txn_id).status, "ON_SELECT")

        init = {"facility": origin, "assignedFacilityId": target, "patient": str(patient.external_id), "title": "Heart"}
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
        self.assertEqual(assigned_referral.patient.name, "Ravi")
        self.assertEqual(assigned_referral.availability.facility, self.target)

        self.bap(txn_id, "status")
        self.assertEqual(self.bap_txn(txn_id).status, "ON_STATUS")
        status = self.poll(txn_id, "on_status")["data"]["message"]["contract"]
        self.assertEqual(status["status"]["code"], "ACTIVE")
        self.assertEqual(status["contractAttributes"]["referral"]["status"], "in_progress")

        poll = self.poll(txn_id)
        self.assertEqual(poll["referralId"], str(origin_referral.external_id))
        self.assertEqual(poll["contractId"], str(assigned_referral.external_id))
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
        txn_id = self.bap().json()["transactionId"]
        self.on_discover(txn_id, bppId="cc.bpp", bppUri="http://cc.bpp")
        body = {
            "facility": str(self.origin.external_id),
            "assignedFacilityId": str(self.target.external_id),
            "patientName": "Asha",
        }
        self.bap(txn_id, "confirm", body)
        appointment = BecknTransaction.objects.create(
            transaction_id="appt-1", side=Side.BPP, flow="appointment", coordination_id=txn_id
        )
        booking_fulfilled.send(sender=None, transaction=appointment, booking=None)
        self.assertEqual(lifecycle.current(txn_id, "assigned").status, "completed")
        self.assertEqual(lifecycle.current(txn_id, "origin").status, "completed")
        self.assertEqual(Referral.objects.count(), 2)

    def test_a_referral_created_in_care_travels_the_whole_loop(self):
        self.loopback()
        patient = self.create_patient(name="Asha", gender="female")
        encounter = baker.make(Encounter, patient=patient, facility=self.origin)
        referral = self.create_referral(
            patient=patient, encounter=encounter, availability=self.network_availability(assigned=self.target)
        )
        from care_ccn_be.flows.referral.tasks import submit_referral

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
        self.assertEqual(self.poll(txn_id)["referralId"], txn_id)
