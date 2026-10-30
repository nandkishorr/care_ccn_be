from care.emr.models.resource_request import ResourceRequest

from care_ccn_be import store
from care_ccn_be.flows.referral import lifecycle
from care_ccn_be.flows.referral.models import ReferralLink
from care_ccn_be.flows.registry import FlowRegistry
from care_ccn_be.models import BecknTransaction, Side
from care_ccn_be.tests.referral.base import ReferralTestBase


class LifecycleTests(ReferralTestBase):
    def setUp(self):
        super().setUp()
        self.txn = store.open_bap_transaction(
            FlowRegistry.get("referral"), self.user, transaction_id="coord-1", coordination_id="coord-1"
        )

    def data(self, **kwargs):
        return lifecycle.RequestData(origin_facility=self.origin, title="T", **kwargs)

    def test_ensure_is_idempotent_on_coordination_id_and_role(self):
        first = lifecycle.ensure_request(self.txn, lifecycle.ORIGIN, self.data())
        again = lifecycle.ensure_request(self.txn, lifecycle.ORIGIN, self.data(), approved=True)
        self.assertEqual(first.pk, again.pk)
        self.assertEqual(ResourceRequest.objects.count(), 1)
        self.assertEqual(ReferralLink.objects.count(), 1)
        self.assertEqual(again.status, "approved")

    def test_the_other_side_of_a_loopback_reuses_the_request(self):
        origin = lifecycle.ensure_request(self.txn, lifecycle.ORIGIN, self.data())
        bpp = BecknTransaction.objects.create(
            transaction_id="coord-1", side=Side.BPP, flow="referral", coordination_id="coord-1"
        )
        reused = lifecycle.ensure_request(bpp, lifecycle.ORIGIN, self.data())
        self.assertEqual(origin.pk, reused.pk)
        self.assertEqual(ReferralLink.objects.filter(role="origin").count(), 1)

    def test_roles_are_independent(self):
        origin = lifecycle.ensure_request(self.txn, lifecycle.ORIGIN, self.data())
        assigned = lifecycle.ensure_request(self.txn, lifecycle.ASSIGNED, self.data(assigned_facility=self.target))
        self.assertNotEqual(origin.pk, assigned.pk)
        self.assertEqual(lifecycle.current("coord-1", lifecycle.ORIGIN).pk, origin.pk)
        self.assertEqual(lifecycle.current("coord-1").pk, assigned.pk)

    def test_missing_coordination_id_on_the_transaction_is_bound(self):
        txn = BecknTransaction.objects.create(transaction_id="t-9", side=Side.BPP, flow="referral")
        request = lifecycle.ensure_request(txn, lifecycle.ORIGIN, self.data())
        txn.refresh_from_db()
        self.assertEqual(txn.coordination_id, "t-9")
        self.assertEqual(lifecycle.current("t-9", lifecycle.ORIGIN).pk, request.pk)

    def test_requests_are_created_with_signals_suppressed_and_owned_by_the_system_user(self):
        request = lifecycle.ensure_request(self.txn, lifecycle.ORIGIN, self.data())
        self.assertEqual(request.created_by, self.system)
        self.assertEqual(request.extensions["beckn"]["role"], "origin")
        self.assertEqual(request.status, "pending")

    def test_complete_is_idempotent(self):
        lifecycle.ensure_request(self.txn, lifecycle.ORIGIN, self.data(), approved=True)
        self.assertEqual(lifecycle.complete("coord-1", lifecycle.ORIGIN).status, "completed")
        self.assertIsNone(lifecycle.complete("coord-1", lifecycle.ORIGIN))
        self.assertIsNone(lifecycle.complete("nope", lifecycle.ORIGIN))

    def test_adopt_links_an_existing_request_once(self):
        request = self.create_request()
        lifecycle.adopt(self.txn, lifecycle.ORIGIN, request)
        lifecycle.adopt(self.txn, lifecycle.ORIGIN, request)
        self.assertEqual(ReferralLink.objects.count(), 1)
        self.assertEqual(lifecycle.origin_request_id(self.txn), str(request.external_id))

    def test_inactive_links_are_ignored(self):
        lifecycle.ensure_request(self.txn, lifecycle.ORIGIN, self.data())
        ReferralLink.objects.update(active=False)
        self.assertIsNone(lifecycle.current("coord-1", lifecycle.ORIGIN))
