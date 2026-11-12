from care.emr.models.scheduling.booking import TokenBooking, TokenSlot

from care_ccn_be.events import booking_fulfilled
from care_ccn_be.models import BecknMessage, Delivery, Processing
from care_ccn_be.models.appointment import AppointmentLink
from care_ccn_be.services import scheduling
from care_ccn_be.tests.appointment.base import BPP, AppointmentTestBase


class AppointmentLoopbackTests(AppointmentTestBase):
    def discover_and_select(self):
        self.loopback()
        txn_id = self.bap().json()["transaction_id"]
        catalogs = self.message(txn_id, "on_discover")["message"]["catalogs"]
        resource_id = catalogs[0]["resources"][0]["id"]
        response = self.bap(
            txn_id, "select", {"context": BPP, "resource_id": resource_id, "date": self.day.isoformat()}
        )
        self.assertEqual(response.status_code, 202, response.content)
        return txn_id, resource_id

    def offered_slots(self, txn_id):
        contract = self.message(txn_id, "on_select")["message"]["contract"]
        return contract["performance"]

    def test_discover_returns_the_published_catalog(self):
        self.loopback()
        txn_id = self.bap().json()["transaction_id"]
        self.assertEqual(self.bap_txn(txn_id).status, "ON_DISCOVER")
        (catalog,) = self.message(txn_id, "on_discover")["message"]["catalogs"]
        self.assertEqual(catalog["id"], f"catalog-{self.facility.external_id}")
        self.assertEqual(catalog["resources"][0]["id"], str(self.resource.external_id))

    def test_select_lists_the_available_slots_of_the_day(self):
        txn_id, resource_id = self.discover_and_select()
        self.assertEqual(self.bap_txn(txn_id).status, "ON_SELECT")
        slots = self.offered_slots(txn_id)
        self.assertEqual(len(slots), 4)
        self.assertEqual({s["id"] for s in slots}, {str(s.external_id) for s in self.slots()})
        state = self.bap_txn(txn_id).state
        self.assertEqual(state["resource_id"], resource_id)
        self.assertEqual(set(state["slots"]), {s["id"] for s in slots})

    def test_init_then_confirm_books_the_slot(self):
        txn_id, _ = self.discover_and_select()
        slot = self.offered_slots(txn_id)[1]["id"]
        patient = str(self.patient.external_id)
        self.assertEqual(self.bap(txn_id, "init", {"slot_id": slot, "patient": patient}).status_code, 202)
        self.assertEqual(self.bap_txn(txn_id).status, "ON_INIT")
        self.assertEqual(TokenBooking.objects.count(), 0)

        self.bap(txn_id, "confirm", {"slot_id": slot, "patient": patient})
        bap, bpp = self.bap_txn(txn_id), self.bpp_txn(txn_id)
        booking = TokenBooking.objects.get()
        self.assertEqual((bap.status, bpp.status), ("ON_CONFIRM", "ON_CONFIRM"))
        self.assertEqual(booking.status, "booked")
        self.assertEqual(str(booking.token_slot.external_id), slot)
        self.assertEqual(booking.patient, self.patient)
        self.assertEqual((bap.contract_id, bpp.contract_id), (str(booking.external_id),) * 2)
        self.assertEqual(booking.token_slot.allocated, 1)
        confirmed = self.message(txn_id, "on_confirm")["message"]["contract"]
        self.assertEqual(confirmed["status"]["code"], "ACTIVE")
        self.assertEqual(confirmed["id"], str(booking.external_id))
        self.assertEqual(self.poll(txn_id)["contract_id"], str(booking.external_id))
        self.assertEqual(AppointmentLink.objects.get().token_booking, booking)
        self.assertFalse(BecknMessage.objects.exclude(delivery_status__in=["", Delivery.ACKED]).exists())
        self.assertFalse(BecknMessage.objects.filter(processing_status=Processing.PENDING).exists())

    def book(self):
        txn_id, _ = self.discover_and_select()
        slots = [s["id"] for s in self.offered_slots(txn_id)]
        body = {"slot_id": slots[0], "patient": str(self.patient.external_id)}
        self.bap(txn_id, "init", body)
        self.bap(txn_id, "confirm", body)
        return txn_id, slots

    def test_status_returns_the_booking(self):
        txn_id, _ = self.book()
        self.assertEqual(self.bap(txn_id, "status").status_code, 202)
        contract = self.message(txn_id, "on_status")["message"]["contract"]
        self.assertEqual(contract["id"], str(TokenBooking.objects.get().external_id))
        self.assertEqual(contract["status"]["code"], "ACTIVE")

    def test_reschedule_moves_the_booking_to_the_new_slot(self):
        txn_id, slots = self.book()
        old = TokenBooking.objects.get()
        self.assertEqual(self.bap(txn_id, "update", {"slot_id": slots[2]}).status_code, 202)
        old.refresh_from_db()
        new = TokenBooking.objects.exclude(pk=old.pk).get()
        self.assertEqual(old.status, "rescheduled")
        self.assertEqual(old.token_slot.allocated, 0)
        self.assertEqual((new.status, str(new.token_slot.external_id)), ("booked", slots[2]))
        self.assertEqual(self.booking_of(self.bpp_txn(txn_id)), new)
        self.assertEqual(self.bap_txn(txn_id).contract_id, str(new.external_id))
        self.assertEqual(self.bpp_txn(txn_id).contract_id, str(new.external_id))
        self.assertEqual(AppointmentLink.objects.filter(active=True).count(), 1)
        self.assertEqual(self.bap_txn(txn_id).status, "ON_UPDATE")

    def test_cancel_frees_the_slot(self):
        txn_id, slots = self.book()
        self.assertEqual(self.bap(txn_id, "cancel", {"reason": "patient unwell"}).status_code, 202)
        booking = TokenBooking.objects.get()
        self.assertEqual(booking.status, "cancelled")
        self.assertEqual(booking.note, "patient unwell")
        self.assertEqual(TokenSlot.objects.get(external_id=slots[0]).allocated, 0)
        contract = self.message(txn_id, "on_cancel")["message"]["contract"]
        self.assertEqual(contract["status"]["code"], "CANCELLED")
        self.assertEqual(self.bap_txn(txn_id).status, "ON_CANCEL")

    def test_full_lifecycle_with_fulfilment(self):
        txn_id, slots = self.book()
        fulfilled = []
        booking_fulfilled.connect(lambda sender, **kw: fulfilled.append(kw), weak=False, dispatch_uid="test.fulfilled")
        self.addCleanup(booking_fulfilled.disconnect, dispatch_uid="test.fulfilled")
        self.bap(txn_id, "update", {"slot_id": slots[1]})
        booking = self.booking_of(self.bpp_txn(txn_id))
        booking.status = "fulfilled"
        booking.save()
        self.assertEqual([kw["booking"] for kw in fulfilled], [booking])
        self.assertEqual(fulfilled[0]["transaction"], self.bpp_txn(txn_id))
        contract = self.message(txn_id, "on_status")["message"]["contract"]
        self.assertEqual(contract["status"]["code"], "COMPLETED")

    def test_manual_review_booking_is_a_draft_until_the_coordinator_approves(self):
        self.loopback()
        review = self.make_resource(self.create_user(username="dr.review"))
        self.make_schedule(review, meta={"beckn": {"acceptanceMode": "MANUAL_REVIEW"}})
        txn_id = self.bap().json()["transaction_id"]
        self.bap(
            txn_id, "select", {"context": BPP, "resource_id": str(review.external_id), "date": self.day.isoformat()}
        )
        slot = self.offered_slots(txn_id)[0]["id"]
        body = {"slot_id": slot, "patient": str(self.patient.external_id)}
        self.bap(txn_id, "init", body)
        self.bap(txn_id, "confirm", body)
        booking = TokenBooking.objects.get()
        self.assertEqual(booking.status, "pending")
        self.assertEqual(self.message(txn_id, "on_confirm")["message"]["contract"]["status"]["code"], "DRAFT")

        scheduling.approve(booking)
        contract = self.message(txn_id, "on_status")["message"]["contract"]
        self.assertEqual(contract["status"]["code"], "ACTIVE")

    def test_care_side_cancellation_reaches_the_bap(self):
        txn_id, _ = self.book()
        booking = TokenBooking.objects.get()
        scheduling.cancel(booking, self.doctor)
        contract = self.message(txn_id, "on_status")["message"]["contract"]
        self.assertEqual(contract["status"]["code"], "CANCELLED")
        self.assertEqual(contract["id"], str(booking.external_id))
        self.assertFalse(BecknMessage.objects.exclude(delivery_status__in=["", Delivery.ACKED]).exists())
