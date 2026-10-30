from unittest import mock

from care.emr.models.resource_request import ResourceRequest
from django.test import override_settings
from model_bakery import baker

from care_ccn_be.context_vars import suppress_beckn_signals
from care_ccn_be.events import booking_fulfilled
from care_ccn_be.flows.referral import lifecycle
from care_ccn_be.flows.referral.models import ReferralLink
from care_ccn_be.models import BecknMessage, BecknTransaction, Direction, Side
from care_ccn_be.tests.framework.base import PLUGIN
from care_ccn_be.tests.referral.base import CC, ReferralTestBase


class ResourceRequestSignalTests(ReferralTestBase):
    def setUp(self):
        super().setUp()
        self.stub_delivery()

    def make(self, **kwargs):
        defaults = {"origin_facility": self.origin, "status": "pending", "category": "other", "extensions": {}}
        return baker.make(ResourceRequest, **{**defaults, **kwargs})

    def test_a_new_pending_request_is_sent_to_the_coordination_centre(self):
        patient = self.create_patient(name="Asha", gender="female")
        request = self.make(title="Heart", related_patient=patient, created_by=self.user)
        self.assertEqual([a for _, a, _ in self.sent], ["confirm"])
        _, _, payload = self.sent[0]
        self.assertEqual(payload["context"]["transactionId"], str(request.external_id))
        self.assertEqual((payload["context"]["bppId"], payload["context"]["bppUri"]), ("cc.bpp", "http://cc.bpp"))
        contract = payload["message"]["contract"]
        self.assertEqual(contract["contractAttributes"]["coordinationId"], str(request.external_id))
        txn = BecknTransaction.objects.get(side=Side.BAP)
        self.assertEqual((txn.flow, txn.coordination_id), ("referral", str(request.external_id)))
        self.assertEqual(ReferralLink.objects.get().role, "origin")
        self.assertEqual(lifecycle.current(str(request.external_id), "origin"), request)

    def test_the_target_extension_overrides_the_destination(self):
        extensions = {"beckn": {"target": {"bppId": "bpp.other", "bppUri": "http://bpp.other"}}}
        self.make(extensions=extensions)
        self.assertEqual(self.sent[0][2]["context"]["bppId"], "bpp.other")
        self.assertEqual(BecknTransaction.objects.get().counterparty_uri, "http://bpp.other")

    def test_other_categories_statuses_and_updates_are_ignored(self):
        self.make(category="comfort_devices")
        self.make(status="approved")
        request = self.make()
        self.sent.clear()
        request.title = "changed"
        request.save()
        self.assertEqual(self.sent, [])

    def test_beckn_created_requests_never_loop_back(self):
        with suppress_beckn_signals():
            self.make()
        self.make(extensions={"beckn": {"role": "assigned"}})
        self.assertEqual(self.sent, [])

    def test_plugin_writes_are_suppressed(self):
        message = self.referral_message(facility=self.origin)
        self.request("init", "coord-1", message)
        self.assertEqual([a for _, a, _ in self.sent], ["on_init"])

    def test_a_disabled_flow_does_not_react(self):
        with override_settings(PLUGIN_CONFIGS={"care_ccn_be": {**PLUGIN, **CC, "BECKN_ENABLED_FLOWS": ["echo"]}}):
            self.make()
        self.assertEqual(self.sent, [])

    def test_the_task_is_idempotent(self):
        from care_ccn_be.flows.referral.tasks import submit_referral

        request = self.make()
        self.assertEqual(submit_referral(request.pk), "skipped")
        self.assertEqual(BecknTransaction.objects.count(), 1)

    def test_a_deleted_request_is_skipped(self):
        from care_ccn_be.flows.referral.tasks import submit_referral

        self.assertEqual(submit_referral(0), "skipped")


class BookingFulfilledTests(ReferralTestBase):
    def setUp(self):
        super().setUp()
        self.stub_delivery()
        self.request("confirm", "coord-1", self.referral_message(facility=self.origin, assigned=self.target))
        self.sent.clear()
        self.appointment_txn = BecknTransaction.objects.create(
            transaction_id="appt-1", side=Side.BPP, flow="appointment", coordination_id="coord-1"
        )

    def fulfil(self, txn=None):
        booking_fulfilled.send(sender=None, transaction=txn or self.appointment_txn, booking=mock.Mock())

    def test_a_fulfilled_booking_completes_the_assigned_request_and_notifies_the_origin(self):
        self.fulfil()
        self.assertEqual(lifecycle.current("coord-1", "assigned").status, "completed")
        (_, action, payload) = self.sent[0]
        self.assertEqual(action, "on_update")
        self.assertEqual(payload["context"]["bapId"], "bap.remote")
        self.assertEqual(payload["message"]["contract"]["status"]["code"], "COMPLETED")
        self.assertEqual(payload["message"]["contract"]["contractAttributes"]["lifecycleState"], "FULFILLED")

    def test_it_is_a_no_op_without_a_coordination_reference(self):
        txn = BecknTransaction.objects.create(transaction_id="appt-2", side=Side.BPP, flow="appointment")
        self.fulfil(txn)
        self.assertEqual(self.sent, [])
        self.assertEqual(lifecycle.current("coord-1", "assigned").status, "approved")

    def test_an_unknown_referral_is_a_no_op(self):
        self.appointment_txn.coordination_id = "other"
        self.fulfil()
        self.assertEqual(self.sent, [])

    def test_it_is_idempotent(self):
        self.fulfil()
        self.fulfil()
        self.assertEqual([a for _, a, _ in self.sent], ["on_update"])
        self.assertEqual(BecknMessage.objects.filter(direction=Direction.OUT, action="on_update").count(), 1)

    def test_without_return_routing_the_request_completes_but_nothing_is_sent(self):
        BecknTransaction.objects.filter(transaction_id="coord-1", side=Side.BPP).update(
            counterparty_id="", counterparty_uri=""
        )
        self.fulfil()
        self.assertEqual(lifecycle.current("coord-1", "assigned").status, "completed")
        self.assertEqual(self.sent, [])
