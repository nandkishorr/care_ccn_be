from care.emr.models.referral import Referral

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
        self.patient = self.create_patient(name="Asha", gender="female")

    def data(self, **kwargs):
        return lifecycle.ReferralData(
            patient=self.patient, availability=self.network_availability(), title="T", **kwargs
        )

    def test_ensure_is_idempotent_on_coordination_id_and_role(self):
        first = lifecycle.ensure_referral(self.txn, lifecycle.ORIGIN, self.data())
        again = lifecycle.ensure_referral(self.txn, lifecycle.ORIGIN, self.data(), approved=True)
        self.assertEqual(first.pk, again.pk)
        self.assertEqual(Referral.objects.count(), 1)
        self.assertEqual(ReferralLink.objects.count(), 1)
        self.assertEqual(again.status, "in_progress")

    def test_the_other_side_of_a_loopback_reuses_the_referral(self):
        origin = lifecycle.ensure_referral(self.txn, lifecycle.ORIGIN, self.data())
        bpp = BecknTransaction.objects.create(
            transaction_id="coord-1", side=Side.BPP, flow="referral", coordination_id="coord-1"
        )
        reused = lifecycle.ensure_referral(bpp, lifecycle.ORIGIN, self.data())
        self.assertEqual(origin.pk, reused.pk)
        self.assertEqual(ReferralLink.objects.filter(role="origin").count(), 1)

    def test_roles_are_independent(self):
        origin = lifecycle.ensure_referral(self.txn, lifecycle.ORIGIN, self.data())
        assigned = lifecycle.ensure_referral(self.txn, lifecycle.ASSIGNED, self.data())
        self.assertNotEqual(origin.pk, assigned.pk)
        self.assertEqual(lifecycle.current("coord-1", lifecycle.ORIGIN).pk, origin.pk)
        self.assertEqual(lifecycle.current("coord-1").pk, assigned.pk)

    def test_missing_coordination_id_on_the_transaction_is_bound(self):
        txn = BecknTransaction.objects.create(transaction_id="t-9", side=Side.BPP, flow="referral")
        referral = lifecycle.ensure_referral(txn, lifecycle.ORIGIN, self.data())
        txn.refresh_from_db()
        self.assertEqual(txn.coordination_id, "t-9")
        self.assertEqual(lifecycle.current("t-9", lifecycle.ORIGIN).pk, referral.pk)

    def test_referrals_are_created_with_signals_suppressed_and_owned_by_the_system_user(self):
        referral = lifecycle.ensure_referral(self.txn, lifecycle.ORIGIN, self.data())
        self.assertEqual(referral.created_by, self.system)
        self.assertEqual(referral.status, "pending")
        self.assertEqual(self.sent, [])

    def test_the_link_keeps_what_the_referral_has_no_column_for(self):
        data = self.data(urgency="URGENT", origin_facility_id="fac-1", contact_name="Dr Who", contact_number="123")
        lifecycle.ensure_referral(self.txn, lifecycle.ORIGIN, data)
        self.assertEqual(
            ReferralLink.objects.get().details,
            {"urgency": "URGENT", "originFacilityId": "fac-1", "contact": {"name": "Dr Who", "number": "123"}},
        )

    def test_assignees_are_stored_with_the_cached_user_ids(self):
        member = {"user_id": self.user.id, "role": {"code": "doctor"}}
        referral = lifecycle.ensure_referral(self.txn, lifecycle.ASSIGNED, self.data(assignees=[member]))
        self.assertEqual(referral.assignees, [member])
        self.assertEqual(referral.assignee_users, [self.user.id])

    def test_complete_is_idempotent(self):
        lifecycle.ensure_referral(self.txn, lifecycle.ORIGIN, self.data(), approved=True)
        self.assertEqual(lifecycle.complete("coord-1", lifecycle.ORIGIN).status, "completed")
        self.assertIsNone(lifecycle.complete("coord-1", lifecycle.ORIGIN))
        self.assertIsNone(lifecycle.complete("nope", lifecycle.ORIGIN))

    def test_adopt_links_an_existing_referral_once(self):
        referral = self.create_referral()
        lifecycle.adopt(self.txn, lifecycle.ORIGIN, referral)
        lifecycle.adopt(self.txn, lifecycle.ORIGIN, referral)
        self.assertEqual(ReferralLink.objects.count(), 1)
        self.assertEqual(lifecycle.origin_referral_id(self.txn), str(referral.external_id))

    def test_inactive_links_are_ignored(self):
        lifecycle.ensure_referral(self.txn, lifecycle.ORIGIN, self.data())
        ReferralLink.objects.update(active=False)
        self.assertIsNone(lifecycle.current("coord-1", lifecycle.ORIGIN))
