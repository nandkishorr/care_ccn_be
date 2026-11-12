from unittest import mock

from care.emr.models import Encounter
from care.emr.models.referral import Referral
from django.test import override_settings
from model_bakery import baker

from care_ccn_be.context_vars import suppress_beckn_signals
from care_ccn_be.events import booking_fulfilled
from care_ccn_be.models import Adapter, BecknMessage, BecknTransaction, Direction
from care_ccn_be.models.referral import ReferralLink
from care_ccn_be.services import availability as availabilities
from care_ccn_be.services import referral as lifecycle
from care_ccn_be.tests.framework.base import PLUGIN
from care_ccn_be.tests.referral.base import CC, REFERRAL_PLUGIN, ReferralTestBase


class ReferralSignalTests(ReferralTestBase):
    def setUp(self):
        super().setUp()
        self.stub_delivery()
        self.patient = self.create_patient(name="Asha", gender="female")

    def make(self, **kwargs):
        defaults = {
            "availability": self.network_availability(),
            "patient": self.patient,
            "status": "pending",
            "title": "Heart",
            "note": "",
            "assignees": [],
            "created_by": self.user,
        }
        return baker.make(Referral, **{**defaults, **kwargs})

    def test_a_new_pending_referral_on_a_network_availability_is_sent_to_its_target(self):
        referral = self.make(title="Heart", note="pain")
        self.assertEqual([a for _, a, _ in self.sent], ["confirm"])
        _, _, payload = self.sent[0]
        self.assertEqual(payload["context"]["transactionId"], str(referral.external_id))
        self.assertEqual((payload["context"]["bppId"], payload["context"]["bppUri"]), ("cc.bpp", "http://cc.bpp"))
        contract = payload["message"]["contract"]
        self.assertEqual(contract["contractAttributes"]["coordinationId"], str(referral.external_id))
        self.assertEqual(contract["descriptor"], {"name": "Heart", "shortDesc": "pain"})
        txn = BecknTransaction.objects.get(adapter=Adapter.BAP)
        self.assertEqual((txn.flow, txn.coordination_id), ("referral", str(referral.external_id)))
        self.assertEqual(ReferralLink.objects.get().role, "origin")
        self.assertEqual(lifecycle.current(str(referral.external_id), "origin"), referral)

    def test_the_availability_target_overrides_the_default_destination(self):
        self.make(availability=availabilities.network("bpp.other", "http://bpp.other"))
        self.assertEqual(self.sent[0][2]["context"]["bppId"], "bpp.other")
        self.assertEqual(BecknTransaction.objects.get().counterparty_uri, "http://bpp.other")

    def test_the_encounter_facility_is_the_origin(self):
        encounter = baker.make(Encounter, patient=self.patient, facility=self.origin)
        self.make(encounter=encounter)
        attributes = self.sent[0][2]["message"]["contract"]["contractAttributes"]
        self.assertEqual(attributes["facilityId"], str(self.origin.external_id))

    def test_other_availabilities_statuses_and_updates_are_ignored(self):
        self.make(availability=availabilities.for_facility(self.target))
        self.make(status="in_progress")
        referral = self.make(availability=availabilities.for_facility(self.origin))
        self.sent.clear()
        referral.title = "changed"
        referral.save()
        self.assertEqual(self.sent, [])

    def test_unconfigured_referral_types_are_ignored(self):
        with override_settings(PLUGIN_CONFIGS={"care_ccn_be": {**REFERRAL_PLUGIN, "BECKN_REFERRAL_TYPES": ["lab"]}}):
            self.make()
            self.assertEqual(self.sent, [])
            self.make(availability=self.network_availability("lab"))
        self.assertEqual([a for _, a, _ in self.sent], ["confirm"])

    def test_beckn_created_referrals_never_loop_back(self):
        with suppress_beckn_signals():
            self.make()
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
        from care_ccn_be.tasks.referral import submit_referral

        referral = self.make()
        self.assertEqual(submit_referral(referral.pk), "skipped")
        self.assertEqual(BecknTransaction.objects.count(), 1)

    def test_the_task_skips_referrals_that_are_not_for_the_network(self):
        from care_ccn_be.tasks.referral import submit_referral

        referral = self.create_referral(availability=availabilities.for_facility(self.target))
        self.assertEqual(submit_referral(referral.pk), "skipped")
        self.assertFalse(BecknTransaction.objects.exists())

    def test_a_deleted_referral_is_skipped(self):
        from care_ccn_be.tasks.referral import submit_referral

        self.assertEqual(submit_referral(0), "skipped")


class BookingFulfilledTests(ReferralTestBase):
    def setUp(self):
        super().setUp()
        self.stub_delivery()
        self.request("confirm", "coord-1", self.referral_message(facility=self.origin, assigned=self.target))
        self.sent.clear()
        self.appointment_txn = BecknTransaction.objects.create(
            transaction_id="appt-1", adapter=Adapter.BPP, flow="appointment", coordination_id="coord-1"
        )

    def fulfil(self, txn=None):
        booking_fulfilled.send(sender=None, transaction=txn or self.appointment_txn, booking=mock.Mock())

    def test_a_fulfilled_booking_completes_the_assigned_referral_and_notifies_the_origin(self):
        self.fulfil()
        self.assertEqual(lifecycle.current("coord-1", "assigned").status, "completed")
        (_, action, payload) = self.sent[0]
        self.assertEqual(action, "on_update")
        self.assertEqual(payload["context"]["bapId"], "bap.remote")
        self.assertEqual(payload["message"]["contract"]["status"]["code"], "COMPLETED")
        self.assertEqual(payload["message"]["contract"]["contractAttributes"]["lifecycleState"], "FULFILLED")

    def test_it_is_a_no_op_without_a_coordination_reference(self):
        txn = BecknTransaction.objects.create(transaction_id="appt-2", adapter=Adapter.BPP, flow="appointment")
        self.fulfil(txn)
        self.assertEqual(self.sent, [])
        self.assertEqual(lifecycle.current("coord-1", "assigned").status, "in_progress")

    def test_an_unknown_referral_is_a_no_op(self):
        self.appointment_txn.coordination_id = "other"
        self.fulfil()
        self.assertEqual(self.sent, [])

    def test_it_is_idempotent(self):
        self.fulfil()
        self.fulfil()
        self.assertEqual([a for _, a, _ in self.sent], ["on_update"])
        self.assertEqual(BecknMessage.objects.filter(direction=Direction.OUT, action="on_update").count(), 1)

    def test_without_return_routing_the_referral_completes_but_nothing_is_sent(self):
        BecknTransaction.objects.filter(transaction_id="coord-1", adapter=Adapter.BPP).update(
            counterparty_id="", counterparty_uri=""
        )
        self.fulfil()
        self.assertEqual(lifecycle.current("coord-1", "assigned").status, "completed")
        self.assertEqual(self.sent, [])
