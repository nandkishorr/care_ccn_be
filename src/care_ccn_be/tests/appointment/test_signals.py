from care.emr.models.scheduling.booking import TokenBooking
from django.test import override_settings

from care_ccn_be.context_vars import suppress_beckn_signals
from care_ccn_be.events import booking_fulfilled
from care_ccn_be.models import Delivery, Direction
from care_ccn_be.models.appointment import AppointmentLink
from care_ccn_be.services import scheduling
from care_ccn_be.tasks.appointment import send_appointment_update
from care_ccn_be.tests.appointment.base import APPOINTMENT_PLUGIN, AppointmentTestBase


class BookingSignalTests(AppointmentTestBase):
    def setUp(self):
        super().setUp()
        self.stub_delivery()
        self.fulfilled = []
        booking_fulfilled.connect(self.on_fulfilled, weak=False, dispatch_uid="test.appointment.fulfilled")
        self.addCleanup(booking_fulfilled.disconnect, dispatch_uid="test.appointment.fulfilled")
        self.request("confirm", "t-1", self.contract(slot=self.first_slot()))
        self.booking = TokenBooking.objects.get()
        self.txn = self.bpp_txn("t-1")

    def on_fulfilled(self, sender, **kwargs):
        self.fulfilled.append(kwargs)

    def sent_actions(self):
        return [m.action for m in self.messages(self.txn, direction=Direction.OUT)]

    def last(self):
        return self.messages(self.txn, direction=Direction.OUT)[-1]

    def change(self, status):
        booking = TokenBooking.objects.get(pk=self.booking.pk)
        booking.status = status
        booking.save()
        return booking

    def test_the_plugins_own_booking_is_not_echoed_back(self):
        self.assertEqual(self.sent_actions(), ["on_confirm"])

    def test_a_cancellation_in_care_is_reported_to_the_bap(self):
        self.change("cancelled")
        message = self.last()
        self.assertEqual(message.action, "on_status")
        self.assertEqual(message.payload["message"]["contract"]["status"]["code"], "CANCELLED")
        self.assertEqual(message.payload["message"]["contract"]["id"], str(self.booking.external_id))
        self.assertEqual(message.delivery_status, Delivery.ACKED)
        self.assertEqual(message.payload["context"]["bapId"], "bap.remote")
        self.assertIsNone(message.reply_to)

    def test_fulfilment_is_reported_and_announced(self):
        booking = self.change("fulfilled")
        self.assertEqual(self.last().payload["message"]["contract"]["status"]["code"], "COMPLETED")
        self.assertEqual(len(self.fulfilled), 1)
        self.assertEqual(self.fulfilled[0]["booking"], booking)
        self.assertEqual(self.fulfilled[0]["transaction"], self.txn)

    def test_only_fulfilment_sends_the_fulfilled_event(self):
        self.change("cancelled")
        self.assertEqual(self.fulfilled, [])

    def test_entered_in_error_is_reported(self):
        self.change("entered_in_error")
        self.assertEqual(self.last().payload["message"]["contract"]["status"]["code"], "CANCELLED")

    def test_other_statuses_and_repeated_saves_are_not_reported(self):
        self.change("arrived")
        self.change("booked")
        booking = TokenBooking.objects.get(pk=self.booking.pk)
        booking.note = "edited"
        booking.save()
        self.assertEqual(self.sent_actions(), ["on_confirm", "on_status"])

    def test_a_reschedule_in_care_reports_the_new_booking_as_an_update(self):
        new_slot = self.slots()[2]
        booking = TokenBooking.objects.get(pk=self.booking.pk)
        replacement = scheduling.reschedule(booking, new_slot, self.doctor)
        self.assertEqual(self.sent_actions(), ["on_confirm", "on_update"])
        message = self.last()
        self.assertEqual(message.payload["message"]["contract"]["id"], str(replacement.external_id))
        self.assertEqual(message.payload["message"]["contract"]["status"]["code"], "ACTIVE")
        link = AppointmentLink.objects.get(active=True)
        self.assertEqual(link.token_booking, replacement)
        self.assertEqual(link.beckn_ref, str(replacement.external_id))
        self.txn.refresh_from_db()
        self.assertEqual(self.txn.contract_id, str(replacement.external_id))
        self.assertEqual(AppointmentLink.objects.count(), 1)

    def test_a_reschedule_does_not_report_a_cancellation(self):
        booking = TokenBooking.objects.get(pk=self.booking.pk)
        scheduling.reschedule(booking, self.slots()[2], self.doctor)
        self.assertNotIn("on_status", self.sent_actions())

    def test_the_replacement_booking_is_tracked_afterwards(self):
        booking = TokenBooking.objects.get(pk=self.booking.pk)
        replacement = scheduling.reschedule(booking, self.slots()[2], self.doctor)
        replacement.status = "cancelled"
        replacement.save()
        message = self.last()
        self.assertEqual(
            (message.action, message.payload["message"]["contract"]["id"]), ("on_status", str(replacement.external_id))
        )

    def test_a_manual_review_approval_is_reported(self):
        review = self.make_resource(self.create_user(username="dr.review"))
        self.make_schedule(review, meta={"beckn": {"acceptanceMode": "MANUAL_REVIEW"}})
        self.request("confirm", "t-2", self.contract(resource=review, slot=self.first_slot(review)), message_id="m-2")
        txn = self.bpp_txn("t-2")
        held = AppointmentLink.objects.get(transaction=txn).token_booking
        self.assertEqual(held.status, "pending")
        before = self.messages(txn, direction=Direction.OUT)
        scheduling.approve(held)
        sent = self.messages(txn, direction=Direction.OUT)
        self.assertEqual(len(sent), len(before) + 1)
        self.assertEqual(sent[-1].action, "on_status")
        self.assertEqual(sent[-1].payload["message"]["contract"]["status"]["code"], "ACTIVE")

    def test_suppressed_writes_are_not_reported(self):
        with suppress_beckn_signals():
            self.change("cancelled")
            self.change("fulfilled")
        self.assertEqual(self.sent_actions(), ["on_confirm"])
        self.assertEqual(self.fulfilled, [])

    def test_a_reschedule_made_under_suppression_is_not_reported(self):
        booking = TokenBooking.objects.get(pk=self.booking.pk)
        with suppress_beckn_signals():
            scheduling.reschedule(booking, self.slots()[2], self.doctor)
        self.assertEqual(self.sent_actions(), ["on_confirm"])

    def test_a_disabled_flow_reports_nothing(self):
        with override_settings(PLUGIN_CONFIGS={"care_ccn_be": {**APPOINTMENT_PLUGIN, "BECKN_ENABLED_FLOWS": ["echo"]}}):
            self.change("cancelled")
        self.assertEqual(self.sent_actions(), ["on_confirm"])
        self.assertEqual(self.fulfilled, [])

    def test_bookings_without_a_beckn_link_are_ignored(self):
        patient = self.create_patient(name="Walk In", gender="male", phone_number="9000000002")
        own = scheduling.book(self.slots()[2], patient, self.doctor)
        own.status = "cancelled"
        own.save()
        self.assertEqual(self.sent_actions(), ["on_confirm"])

    def test_a_replaced_booking_is_no_longer_tracked(self):
        booking = TokenBooking.objects.get(pk=self.booking.pk)
        scheduling.reschedule(booking, self.slots()[2], self.doctor)
        before = self.sent_actions()
        old = TokenBooking.objects.get(pk=self.booking.pk)
        old.status = "entered_in_error"
        old.save()
        self.assertEqual(self.sent_actions(), before)

    def test_the_notify_task_skips_an_inactive_link(self):
        link = AppointmentLink.objects.get()
        link.active = False
        link.save()
        self.assertEqual(send_appointment_update(link.pk, "status"), "skipped")
        self.assertEqual(send_appointment_update(0, "status"), "skipped")
