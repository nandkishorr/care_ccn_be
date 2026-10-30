from care.emr.models import Patient
from care.emr.models.scheduling.booking import TokenBooking

from care_ccn_be.flows.appointment.models import AppointmentLink
from care_ccn_be.models import Delivery, Direction, Processing
from care_ccn_be.services import scheduling
from care_ccn_be.tests.appointment.base import AppointmentTestBase


class BppTestBase(AppointmentTestBase):
    def setUp(self):
        super().setUp()
        self.stub_delivery()
        self.counter = 0

    def call(self, action, txn_id, message):
        self.counter += 1
        response = self.request(action, txn_id, message, message_id=f"m-{self.counter}")
        self.assertEqual(response.json(), {"message": {"ack": {"status": "ACK"}}}, response.content)
        return self.callback(txn_id)

    def callback(self, txn_id):
        return self.messages(self.bpp_txn(txn_id), direction=Direction.OUT)[-1].payload

    def assertError(self, payload, code):
        self.assertEqual(payload.get("error", {}).get("code"), code, payload)

    def confirm(self, txn_id="t-1", slot=None, **attributes):
        slot = slot or self.first_slot()
        payload = self.call("confirm", txn_id, self.contract(slot=slot, **attributes))
        return slot, payload


class SelectTests(BppTestBase):
    def message(self, resource_id=None, **intent):
        message = self.contract()
        if resource_id is not None:
            message["contract"]["commitments"][0]["resources"][0]["id"] = resource_id
        if intent:
            message["intent"] = intent
        return message

    def test_lists_the_slots_of_the_requested_day(self):
        payload = self.call("select", "t-1", self.message(preferredDate=self.day.isoformat()))
        slots = payload["message"]["contract"]["performance"]
        self.assertEqual([s["id"] for s in slots], [str(s.external_id) for s in self.slots()])
        self.assertEqual(payload["message"]["contract"]["status"]["code"], "DRAFT")
        self.assertEqual(self.bpp_txn("t-1").status, "ON_SELECT")

    def test_a_day_without_slots_is_an_empty_offer(self):
        other = self.day.replace(year=self.day.year + 1)
        payload = self.call("select", "t-1", self.message(date=other.isoformat()))
        self.assertEqual(payload["message"]["contract"]["performance"], [])

    def test_the_day_defaults_to_the_performance_window(self):
        message = self.message()
        slot = self.first_slot()
        message["contract"]["performance"] = [
            {
                "performanceAttributes": {
                    "@type": "hpe:HealthPerformance",
                    "appointmentWindowStart": slot.start_datetime.isoformat(),
                    "appointmentWindowEnd": slot.end_datetime.isoformat(),
                }
            }
        ]
        payload = self.call("select", "t-1", message)
        self.assertEqual(len(payload["message"]["contract"]["performance"]), 4)

    def test_unknown_resource(self):
        payload = self.call("select", "t-1", self.message("00000000-0000-0000-0000-000000000000"))
        self.assertError(payload, "70004")

    def test_malformed_resource_id(self):
        self.assertError(self.call("select", "t-1", self.message("not-a-uuid")), "70004")

    def test_invalid_requested_date(self):
        self.assertError(self.call("select", "t-1", self.message(preferredDate="soon")), "70002")

    def test_the_error_callback_marks_the_inbound_failed(self):
        self.call("select", "t-1", self.message("00000000-0000-0000-0000-000000000000"))
        inbound = self.messages(self.bpp_txn("t-1"), direction=Direction.IN)[0]
        self.assertEqual(inbound.processing_status, Processing.FAILED)

    def test_the_service_type_follows_the_schedule_meta(self):
        other = self.make_resource(self.create_user(username="dr.lab"))
        self.make_schedule(other, meta={"beckn": {"healthServiceType": "TELE_CONSULTATION"}})
        message = self.contract(resource=other)
        message["contract"]["contractAttributes"].pop("healthServiceType")
        message["intent"] = {"date": self.day.isoformat()}
        payload = self.call("select", "t-1", message)
        performance = payload["message"]["contract"]["performance"][0]
        self.assertEqual(performance["performanceAttributes"]["healthServiceType"], "TELE_CONSULTATION")


class InitTests(BppTestBase):
    def test_answers_a_draft_with_the_slot_and_creates_no_booking(self):
        slot = self.first_slot()
        payload = self.call("init", "t-1", self.contract(slot=slot))
        contract = payload["message"]["contract"]
        self.assertEqual(contract["status"]["code"], "DRAFT")
        self.assertEqual([p["id"] for p in contract["performance"]], [str(slot.external_id)])
        self.assertEqual(TokenBooking.objects.count(), 0)
        self.assertEqual(self.bpp_txn("t-1").status, "ON_INIT")

    def test_reuses_the_matching_patient(self):
        before = Patient.objects.count()
        self.call("init", "t-1", self.contract(slot=self.first_slot()))
        self.assertEqual(Patient.objects.count(), before)

    def test_registers_an_unknown_patient(self):
        message = self.contract(slot=self.first_slot())
        message["contract"]["participants"][0]["descriptor"]["name"] = "Ravi Kumar"
        message["contract"]["participants"][0]["participantAttributes"]["gender"] = "MALE"
        message["contract"]["participants"][0]["participantAttributes"]["healthIds"] = []
        before = Patient.objects.count()
        self.call("init", "t-1", message)
        self.assertEqual(Patient.objects.count(), before + 1)
        patient = Patient.objects.get(name="Ravi Kumar")
        self.assertEqual(patient.gender, "male")
        self.assertEqual(patient.date_of_birth.isoformat(), "1985-04-12")

    def test_requires_a_patient(self):
        payload = self.call("init", "t-1", self.contract(slot=self.first_slot(), patient=False))
        self.assertError(payload, "70002")

    def test_unknown_slot(self):
        message = self.contract(slot=self.first_slot())
        message["contract"]["performance"] = [{"id": "00000000-0000-0000-0000-000000000000"}]
        self.assertError(self.call("init", "t-1", message), "70004")

    def test_missing_slot(self):
        self.assertError(self.call("init", "t-1", self.contract()), "70004")

    def test_slot_of_another_resource(self):
        other = self.make_resource(self.create_user(username="dr.other"))
        self.make_schedule(other)
        payload = self.call("init", "t-1", self.contract(slot=self.first_slot(other)))
        self.assertError(payload, "70002")

    def test_full_slot(self):
        slot = self.first_slot()
        scheduling.book(slot, self.patient, self.doctor)
        self.assertError(self.call("init", "t-1", self.contract(slot=slot)), "70005")

    def test_invalid_contract(self):
        bad = self.contract(slot=self.first_slot())
        bad["contract"]["commitments"] = "nope"
        body = self.request("init", "t-1", bad).json()
        self.assertEqual(body["message"]["ack"]["status"], "NACK")
        self.assertEqual(body["error"]["code"], "70002")


class ConfirmTests(BppTestBase):
    def test_books_the_slot_and_links_it_to_the_transaction(self):
        slot, payload = self.confirm()
        booking = TokenBooking.objects.get()
        txn = self.bpp_txn("t-1")
        contract = payload["message"]["contract"]
        self.assertEqual((contract["id"], contract["status"]["code"]), (str(booking.external_id), "ACTIVE"))
        self.assertEqual(booking.token_slot, slot)
        self.assertEqual(booking.status, "booked")
        self.assertEqual(booking.patient, self.patient)
        self.assertEqual(booking.booked_by, self.system)
        self.assertEqual(
            (txn.contract_id, txn.patient, txn.facility), (str(booking.external_id), self.patient, self.facility)
        )
        link = AppointmentLink.objects.get()
        self.assertEqual((link.transaction, link.token_booking, link.active), (txn, booking, True))
        confirmed = contract["performance"][0]["performanceAttributes"]["confirmedAppointmentTime"]
        self.assertEqual(confirmed, slot.start_datetime.isoformat())

    def test_a_confirmation_does_not_notify_the_bap_again(self):
        self.confirm()
        actions = [m.action for m in self.messages(self.bpp_txn("t-1"), direction=Direction.OUT)]
        self.assertEqual(actions, ["on_confirm"])

    def test_coordination_reference_is_kept(self):
        self.confirm(coordinationRef="coord-77")
        self.assertEqual(self.bpp_txn("t-1").coordination_id, "coord-77")
        self.assertEqual(TokenBooking.objects.get().note, "coord-77")

    def test_manual_review_holds_the_booking_as_a_draft(self):
        review = self.make_resource(self.create_user(username="dr.review"))
        self.make_schedule(review, meta={"beckn": {"acceptanceMode": "MANUAL_REVIEW"}})
        slot = self.first_slot(review)
        payload = self.call("confirm", "t-1", self.contract(resource=review, slot=slot))
        booking = TokenBooking.objects.get()
        self.assertEqual(booking.status, "pending")
        self.assertEqual(payload["message"]["contract"]["status"]["code"], "DRAFT")
        self.assertEqual(AppointmentLink.objects.get().token_booking, booking)

    def test_full_slot(self):
        slot = self.first_slot()
        scheduling.book(slot, self.patient, self.doctor)
        payload = self.call("confirm", "t-1", self.contract(slot=slot))
        self.assertError(payload, "70005")
        self.assertEqual(AppointmentLink.objects.count(), 0)

    def test_requires_a_patient(self):
        payload = self.call("confirm", "t-1", self.contract(slot=self.first_slot(), patient=False))
        self.assertError(payload, "70002")
        self.assertEqual(TokenBooking.objects.count(), 0)

    def test_unknown_slot(self):
        message = self.contract()
        message["contract"]["performance"] = [{"id": "00000000-0000-0000-0000-000000000000"}]
        self.assertError(self.call("confirm", "t-1", message), "70004")


class StatusCancelUpdateTests(BppTestBase):
    def booked(self):
        slot, _ = self.confirm()
        return slot, TokenBooking.objects.get()

    def test_status_reports_the_booking(self):
        _, booking = self.booked()
        payload = self.call("status", "t-1", {"contract": {"id": str(booking.external_id)}})
        contract = payload["message"]["contract"]
        self.assertEqual((contract["id"], contract["status"]["code"]), (str(booking.external_id), "ACTIVE"))

    def test_status_follows_changes_made_in_care(self):
        _, booking = self.booked()
        booking.status = "fulfilled"
        booking.save()
        payload = self.call("status", "t-1", {"contract": {"id": str(booking.external_id)}})
        self.assertEqual(payload["message"]["contract"]["status"]["code"], "COMPLETED")

    def opened(self):
        self.call("init", "t-1", self.contract(slot=self.first_slot()))

    def test_status_of_an_unknown_appointment(self):
        self.opened()
        payload = self.call("status", "t-1", {"contract": {"id": "00000000-0000-0000-0000-000000000000"}})
        self.assertError(payload, "70004")

    def test_status_finds_a_booking_by_the_contract_id_without_a_link(self):
        self.opened()
        booking = scheduling.book(self.first_slot(), self.patient, self.doctor)
        payload = self.call("status", "t-1", {"contract": {"id": str(booking.external_id)}})
        self.assertEqual(payload["message"]["contract"]["id"], str(booking.external_id))

    def test_cancel_frees_the_slot_and_records_the_reason(self):
        slot, booking = self.booked()
        message = self.contract(slot=slot, cancellationReason="travelling")
        message["contract"]["id"] = str(booking.external_id)
        payload = self.call("cancel", "t-1", message)
        booking.refresh_from_db()
        slot.refresh_from_db()
        self.assertEqual((booking.status, booking.note, slot.allocated), ("cancelled", "travelling", 0))
        self.assertEqual(payload["message"]["contract"]["status"]["code"], "CANCELLED")

    def test_cancelling_twice_is_refused(self):
        slot, booking = self.booked()
        message = {"contract": {"id": str(booking.external_id)}}
        self.call("cancel", "t-1", message)
        self.assertError(self.call("cancel", "t-1", message), "70005")

    def test_a_booking_in_consultation_cannot_be_cancelled(self):
        _, booking = self.booked()
        booking.status = "in_consultation"
        booking.save()
        self.assertError(self.call("cancel", "t-1", {"contract": {"id": str(booking.external_id)}}), "70005")
        booking.refresh_from_db()
        self.assertEqual(booking.status, "in_consultation")

    def test_cancel_of_an_unknown_appointment(self):
        self.opened()
        payload = self.call("cancel", "t-1", {"contract": {"id": "00000000-0000-0000-0000-000000000000"}})
        self.assertError(payload, "70004")

    def test_update_moves_the_appointment_to_the_new_slot(self):
        old_slot, old = self.booked()
        new_slot = self.slots()[2]
        payload = self.call("update", "t-1", self.contract(slot=new_slot))
        new = TokenBooking.objects.exclude(pk=old.pk).get()
        old.refresh_from_db()
        old_slot.refresh_from_db()
        self.assertEqual((old.status, old_slot.allocated), ("rescheduled", 0))
        self.assertEqual((new.status, new.token_slot, new.patient), ("booked", new_slot, self.patient))
        self.assertEqual(payload["message"]["contract"]["id"], str(new.external_id))
        self.assertEqual(self.bpp_txn("t-1").contract_id, str(new.external_id))
        self.assertEqual(
            list(AppointmentLink.objects.filter(active=True).values_list("token_booking", flat=True)), [new.pk]
        )
        self.assertEqual(AppointmentLink.objects.filter(active=False).count(), 1)

    def test_update_to_the_same_slot_is_refused(self):
        slot, _ = self.booked()
        self.assertError(self.call("update", "t-1", self.contract(slot=slot)), "70002")

    def test_update_to_a_full_slot_keeps_the_original_booking(self):
        old_slot, old = self.booked()
        taken = self.slots()[1]
        other = self.create_patient(name="Someone Else", gender="male", phone_number="9000000001")
        scheduling.book(taken, other, self.doctor)
        self.assertError(self.call("update", "t-1", self.contract(slot=taken)), "70005")
        old.refresh_from_db()
        old_slot.refresh_from_db()
        self.assertEqual((old.status, old_slot.allocated), ("booked", 1))
        self.assertEqual(AppointmentLink.objects.get().token_booking, old)

    def test_update_to_an_unknown_slot(self):
        self.booked()
        message = self.contract()
        message["contract"]["performance"] = [{"id": "00000000-0000-0000-0000-000000000000"}]
        self.assertError(self.call("update", "t-1", message), "70004")

    def test_every_callback_is_delivered(self):
        self.booked()
        sent = self.messages(self.bpp_txn("t-1"), direction=Direction.OUT)
        self.assertEqual({m.delivery_status for m in sent}, {Delivery.ACKED})
