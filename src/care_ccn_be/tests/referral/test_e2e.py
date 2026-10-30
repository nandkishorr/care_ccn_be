from care.emr.models.resource_request import ResourceRequest

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
        self.assertEqual(ResourceRequest.objects.get().status, "pending")

        self.bap(txn_id, "confirm", init)
        self.assertEqual((self.bap_txn(txn_id).status, self.bpp_txn(txn_id).status), ("ON_CONFIRM", "ON_CONFIRM"))
        origin_request = lifecycle.current(txn_id, "origin")
        assigned_request = lifecycle.current(txn_id, "assigned")
        self.assertEqual((origin_request.status, assigned_request.status), ("approved", "approved"))
        self.assertNotEqual(origin_request.pk, assigned_request.pk)
        self.assertEqual(ResourceRequest.objects.count(), 2)
        self.assertEqual(assigned_request.related_patient.name, "Ravi")

        self.bap(txn_id, "status")
        self.assertEqual(self.bap_txn(txn_id).status, "ON_STATUS")
        status = self.poll(txn_id, "on_status")["data"]["message"]["contract"]
        self.assertEqual(status["status"]["code"], "ACTIVE")
        self.assertEqual(status["contractAttributes"]["referral"]["status"], "approved")

        poll = self.poll(txn_id)
        self.assertEqual(poll["resourceRequestId"], str(origin_request.external_id))
        self.assertEqual(poll["contractId"], str(assigned_request.external_id))
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

    def test_a_fulfilled_booking_completes_both_requests(self):
        self.loopback(nack_actions=("discover",))
        txn_id = self.bap().json()["transactionId"]
        self.on_discover(txn_id, bppId="cc.bpp", bppUri="http://cc.bpp")
        body = {"facility": str(self.origin.external_id), "assignedFacilityId": str(self.target.external_id)}
        self.bap(txn_id, "confirm", body)
        appointment = BecknTransaction.objects.create(
            transaction_id="appt-1", side=Side.BPP, flow="appointment", coordination_id=txn_id
        )
        booking_fulfilled.send(sender=None, transaction=appointment, booking=None)
        self.assertEqual(lifecycle.current(txn_id, "assigned").status, "completed")
        self.assertEqual(lifecycle.current(txn_id, "origin").status, "completed")
        self.assertEqual(ResourceRequest.objects.count(), 2)

    def test_a_request_created_in_care_travels_the_whole_loop(self):
        self.loopback()
        patient = self.create_patient(name="Asha", gender="female")
        request = self.create_request(related_patient=patient, assigned_facility=self.target, extensions={})
        from care_ccn_be.flows.referral.tasks import submit_referral

        self.assertEqual(submit_referral(request.pk), "sent")
        txn_id = str(request.external_id)
        self.assertEqual((self.bap_txn(txn_id).status, self.bpp_txn(txn_id).status), ("ON_CONFIRM", "ON_CONFIRM"))
        request.refresh_from_db()
        self.assertEqual(request.status, "approved")
        self.assertEqual(lifecycle.current(txn_id, "origin").pk, request.pk)
        assigned = lifecycle.current(txn_id, "assigned")
        self.assertEqual(assigned.assigned_facility, self.target)
        self.assertEqual(ResourceRequest.objects.count(), 2)
        self.assertEqual(self.poll(txn_id)["resourceRequestId"], txn_id)
