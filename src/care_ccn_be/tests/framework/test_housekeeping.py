from datetime import timedelta
from unittest import mock

from django.utils import timezone

from care_ccn_be.models import BecknMessage, Delivery, Direction, Processing
from care_ccn_be.tasks import purge_beckn_messages, requeue_stuck_messages
from care_ccn_be.tests.framework.base import FrameworkTestBase, inbound_request


class HousekeepingTests(FrameworkTestBase):
    def age(self, *messages, minutes=0, days=0):
        past = timezone.now() - timedelta(minutes=minutes, days=days)
        BecknMessage.objects.filter(pk__in=[m.pk for m in messages]).update(created_date=past, modified_date=past)

    def test_requeue_redelivers_stuck_outbound_and_reprocesses_stuck_inbound(self):
        with (
            mock.patch("care_ccn_be.store.enqueue_delivery"),
            mock.patch("care_ccn_be.tasks.process_bpp_request.apply_async"),
        ):
            txn_id = self.bap().json()["transaction_id"]
            self.anon.post("/bpp/receiver/select", inbound_request("select"), format="json")
        out = BecknMessage.objects.get(direction=Direction.OUT, delivery_status=Delivery.QUEUED)
        inbound = BecknMessage.objects.get(direction=Direction.IN, processing_status=Processing.PENDING)
        self.assertEqual(requeue_stuck_messages(), 0)  # too fresh
        self.age(out, inbound, minutes=30)
        self.stub_delivery()
        self.assertEqual(requeue_stuck_messages(), 2)
        out.refresh_from_db()
        inbound.refresh_from_db()
        self.assertEqual(out.delivery_status, Delivery.ACKED)
        self.assertEqual(inbound.processing_status, Processing.PROCESSED)
        self.assertTrue(txn_id)

    def test_purge_deletes_only_old_messages(self):
        self.stub_delivery()
        self.bap()
        old = BecknMessage.objects.get()
        self.bap()
        self.age(old, days=400)
        self.assertEqual(purge_beckn_messages(), 1)
        self.assertEqual(BecknMessage.objects.count(), 1)
