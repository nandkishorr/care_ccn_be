import datetime
from io import StringIO
from unittest import mock

from care.emr.models.patient import Patient
from care.emr.models.resource_request import ResourceRequest
from care.emr.models.scheduling.booking import TokenBooking
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from django.utils import timezone
from model_bakery import baker

from care_ccn_be.context_vars import suppress_beckn_signals
from care_ccn_be.flows.appointment.models import AppointmentLink
from care_ccn_be.flows.referral.models import ReferralLink
from care_ccn_be.models import BecknMessage, BecknTransaction, Side
from care_ccn_be.services import scheduling
from care_ccn_be.tests.appointment.base import APPOINTMENT_PLUGIN, AppointmentTestBase

PLUGIN = {**APPOINTMENT_PLUGIN, "BECKN_CC_BPP_ID": "cc.bpp", "BECKN_CC_BPP_URI": "http://cc.bpp"}


class FakeCache:
    def __init__(self, data):
        self.data = data

    def iter_keys(self, pattern):
        prefix = pattern.rstrip("*")
        return iter([k for k in self.data if k.startswith(prefix)])

    def get(self, key, default=None):
        return self.data.get(key, default)


@override_settings(PLUGIN_CONFIGS={"care_ccn_be": PLUGIN})
class BackfillTestBase(AppointmentTestBase):
    def run_backfill(self, *args):
        out = StringIO()
        call_command("beckn_backfill", *args, stdout=out)
        return out.getvalue()

    def make_rr(self, beckn=None, **kwargs):
        target = self.create_facility(self.user)
        defaults = {
            "origin_facility": self.facility,
            "assigned_facility": target,
            "status": "pending",
            "category": "other",
            "extensions": {"beckn": beckn} if beckn is not None else {},
        }
        with suppress_beckn_signals():
            return baker.make(ResourceRequest, **{**defaults, **kwargs})

    def make_booking(self, tid=None, status="booked", **beckn):
        slots = self.slots()
        booking = scheduling.book(slots[len([b for b in TokenBooking.objects.all()])], self.patient, self.user)
        meta = {"beckn": {"transactionId": tid, **beckn}} if tid else {}
        TokenBooking.objects.filter(pk=booking.pk).update(meta=meta, status=status)
        booking.refresh_from_db()
        return booking


class ReferralShapesTest(BackfillTestBase):
    def test_confirm_blob_becomes_bpp_transaction_message_and_link(self):
        contract = {"id": "c-1", "status": {"code": "ACTIVE"}}
        rr = self.make_rr(
            {
                "role": "assigned",
                "transactionId": "t-1",
                "coordinationId": "coord-1",
                "contract": contract,
                "returnRouting": {"bapId": "bap.x", "bapUri": "http://bap.x"},
            }
        )
        self.run_backfill()
        txn = BecknTransaction.objects.get(transaction_id="t-1", side=Side.BPP)
        self.assertEqual(
            (txn.flow, txn.status, txn.counterparty_id, txn.coordination_id),
            ("referral", "ON_CONFIRM", "bap.x", "coord-1"),
        )
        self.assertEqual(txn.state["legacy"]["source"], "referral_confirm")
        msg = BecknMessage.objects.get(transaction=txn, action="confirm")
        self.assertEqual(msg.payload["message"]["contract"], contract)
        self.assertEqual(ReferralLink.objects.get(transaction=txn).resource_request, rr)

    def test_init_blob_is_bpp_and_ambiguous_without_a_redis_record(self):
        self.make_rr({"role": "assigned", "transactionId": "t-2", "coordinationId": "coord-2"})
        with mock.patch("care_ccn_be.services.backfill.cache", FakeCache({})):
            out = self.run_backfill()
        self.assertTrue(BecknTransaction.objects.filter(transaction_id="t-2", side=Side.BPP, status="ON_INIT").exists())
        self.assertIn("referral_init", [line for line in out.splitlines() if " 1 " in line][0])

    def test_init_blob_is_bap_when_redis_has_the_transaction(self):
        self.make_rr({"role": "origin", "transactionId": "t-3"})
        with mock.patch("care_ccn_be.services.backfill.cache", FakeCache({"beckn:txn:t-3": {"transactionId": "t-3"}})):
            self.run_backfill()
        self.assertTrue(BecknTransaction.objects.filter(transaction_id="t-3", side=Side.BAP).exists())

    def test_on_confirm_blob_becomes_bap_origin_link(self):
        rr = self.make_rr({"transactionId": "t-4", "contract": {"id": "c-4"}})
        self.run_backfill()
        txn = BecknTransaction.objects.get(transaction_id="t-4", side=Side.BAP)
        self.assertEqual((txn.counterparty_id, txn.coordination_id, txn.contract_id), ("cc.bpp", "t-4", "c-4"))
        link = ReferralLink.objects.get(transaction=txn)
        self.assertEqual((link.role, link.resource_request), ("origin", rr))

    def test_sent_referral_needs_since_and_matches_trigger(self):
        approved = self.make_rr(status="approved")
        pending = self.make_rr()
        self.make_rr(category="comfort_devices")
        self.run_backfill()
        self.assertFalse(ReferralLink.objects.exists())
        self.run_backfill("--since", (timezone.localdate() - datetime.timedelta(days=1)).isoformat())
        self.assertEqual(ReferralLink.objects.count(), 2)
        self.assertEqual(BecknTransaction.objects.get(transaction_id=str(approved.external_id)).status, "ON_CONFIRM")
        self.assertEqual(BecknTransaction.objects.get(transaction_id=str(pending.external_id)).status, "CONFIRM")

    def test_rerun_is_idempotent_and_dry_run_writes_nothing(self):
        self.make_rr({"transactionId": "t-5", "contract": {"id": "c"}})
        self.run_backfill("--dry-run")
        self.assertFalse(BecknTransaction.objects.exists())
        self.run_backfill()
        self.run_backfill()
        self.assertEqual(BecknTransaction.objects.count(), 1)
        self.assertEqual(ReferralLink.objects.count(), 1)


class AppointmentShapesTest(BackfillTestBase):
    def test_booking_blob_becomes_bpp_transaction_with_link_and_message(self):
        context = {"messageId": "m-9", "bapId": "bap.y", "networkId": "net"}
        booking = self.make_booking(
            "t-10", bapId="bap.y", bapUri="http://bap.y", context=context, message={"contract": {"id": "x"}}
        )
        self.run_backfill()
        txn = BecknTransaction.objects.get(transaction_id="t-10", side=Side.BPP)
        self.assertEqual(
            (txn.flow, txn.status, txn.counterparty_id, txn.network_id), ("appointment", "ON_CONFIRM", "bap.y", "net")
        )
        self.assertEqual(txn.contract_id, str(booking.external_id))
        self.assertEqual(AppointmentLink.objects.get(transaction=txn).token_booking, booking)
        self.assertEqual(BecknMessage.objects.get(transaction=txn).action, "confirm")

    def test_reschedule_copies_become_one_transaction_with_one_active_link(self):
        old = self.make_booking("t-11", status="rescheduled", bapId="bap.y")
        new = self.make_booking("t-11", bapId="bap.y")
        self.run_backfill()
        self.run_backfill()
        txn = BecknTransaction.objects.get(transaction_id="t-11")
        links = {link.token_booking_id: link.active for link in AppointmentLink.objects.filter(transaction=txn)}
        self.assertEqual(links, {old.pk: False, new.pk: True})

    def test_redis_booking_referral_sets_coordination_id(self):
        booking = self.make_booking("t-12", bapId="bap.y")
        cache = FakeCache({f"beckn:booking-referral:{booking.external_id}": "coord-12"})
        with mock.patch("care_ccn_be.services.backfill.cache", cache):
            self.run_backfill()
        self.assertEqual(BecknTransaction.objects.get(transaction_id="t-12").coordination_id, "coord-12")

    def test_coordination_ref_falls_back_to_the_stored_message(self):
        self.make_booking("t-13", message={"contract": {"contractAttributes": {"coordinationRef": "coord-13"}}})
        self.run_backfill()
        self.assertEqual(BecknTransaction.objects.get(transaction_id="t-13").coordination_id, "coord-13")


class RedisTransactionsTest(BackfillTestBase):
    def test_redis_records_become_bap_transactions_with_messages(self):
        data = {
            "beckn:txn:t-20": {
                "transactionId": "t-20",
                "serviceType": "appointment",
                "status": "ON_SELECT",
                "routing": {"bppId": "bpp.z", "bppUri": "http://bpp.z"},
                "context": {"bppId": "bpp.z"},
                "actions": ["SELECT", "ON_SELECT"],
            },
            "beckn:txn:t-20:SELECT": {"context": {"messageId": "m-1"}, "message": {}},
            "beckn:txn:t-20:ON_SELECT": {"context": {"messageId": "m-1", "bppId": "bpp.z"}, "message": {}},
            "beckn:txn:t-21": {"transactionId": "t-21", "serviceType": "unknown"},
        }
        with mock.patch("care_ccn_be.services.backfill.cache", FakeCache(data)):
            self.run_backfill("--only", "redis")
            self.run_backfill("--only", "redis")
        txn = BecknTransaction.objects.get(transaction_id="t-20", side=Side.BAP)
        self.assertEqual((txn.flow, txn.status, txn.counterparty_id), ("appointment", "ON_SELECT", "bpp.z"))
        directions = {m.action: (m.direction, m.delivery_status) for m in BecknMessage.objects.filter(transaction=txn)}
        self.assertEqual(directions, {"select": ("out", "acked"), "on_select": ("in", "")})
        self.assertFalse(BecknTransaction.objects.filter(transaction_id="t-21").exists())


class StripLegacyTest(BackfillTestBase):
    def test_refuses_while_rows_have_no_link_unless_forced(self):
        self.make_rr({"role": "origin", "transactionId": "t-30", "contract": {}, "target": {"bppId": "b"}})
        with self.assertRaises(CommandError):
            self.run_backfill("--strip-legacy")
        self.run_backfill("--strip-legacy", "--force")
        rr = ResourceRequest.objects.get()
        self.assertEqual(rr.extensions["beckn"], {"v": 1, "target": {"bppId": "b"}})

    def test_strips_all_three_blobs_once_linked(self):
        rr = self.make_rr({"transactionId": "t-31", "contract": {"id": "c"}})
        booking = self.make_booking("t-31", bapId="bap.y")
        Patient.objects.filter(pk=self.patient.pk).update(
            extensions={"beckn": {"primaryLanguage": "hi", "participant": {"x": 1}}, "other": 1}
        )
        self.run_backfill()
        self.run_backfill("--strip-legacy")
        rr.refresh_from_db()
        booking.refresh_from_db()
        self.patient.refresh_from_db()
        self.assertEqual(rr.extensions["beckn"], {"v": 1})
        self.assertNotIn("beckn", booking.meta)
        self.assertEqual(self.patient.extensions, {"beckn": {"v": 1, "primaryLanguage": "hi"}, "other": 1})
        self.assertEqual(AppointmentLink.objects.count(), 1)
