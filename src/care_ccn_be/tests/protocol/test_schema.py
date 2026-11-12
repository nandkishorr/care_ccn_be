import copy
import json
import pathlib
from datetime import UTC, date, datetime

from django.test import SimpleTestCase
from pydantic import ValidationError

from care_ccn_be.protocol.ack import ack, is_nack, nack, nack_from
from care_ccn_be.protocol.builder import ContractBuilder, Part
from care_ccn_be.resources.beckn import (
    BecknModel,
    BecknRequest,
    Commitment,
    Consideration,
    Contract,
    ContractMessage,
    Descriptor,
    HasConsideration,
    JsonLdModel,
    Offer,
    Participant,
    Performance,
    Price,
    Quantity,
    Resource,
    Status,
)
from care_ccn_be.resources.beckn.health import HealthContract, HealthId, HealthParticipant, HealthPerformance

PAYLOADS = pathlib.Path(__file__).parent / "payloads"
INIT = json.loads((PAYLOADS / "init.json").read_text())
CONFIRM = json.loads((PAYLOADS / "confirm.json").read_text())
PATIENT = "PATIENT"


class BookingContract(Contract):
    @property
    def patient(self):
        return self.participant(PATIENT)

    @property
    def resource_id(self):
        return next((r.id for c in self.commitments for r in c.resources), None)

    @property
    def slot_id(self):
        return next((p.id for p in self.performance if p.id), None)


class BookingMessage(ContractMessage[BookingContract]):
    pass


Request = BecknRequest[BookingMessage]


class SchemaTests(SimpleTestCase):
    def test_typed_access(self):
        contract = Request.model_validate(INIT).message.contract
        attrs = contract.patient.participant_attributes
        self.assertEqual(attrs.health_ids[0].value, "91-1234-5678-9012")
        self.assertEqual(attrs.date_of_birth, date(1985, 4, 12))
        self.assertIsInstance(contract.contract_attributes, HealthContract)
        self.assertIsInstance(contract.performance[0].performance_attributes, HealthPerformance)
        self.assertIsInstance(attrs, HealthParticipant)
        self.assertEqual(contract.resource_id, "7e772edf-4c5f-4707-aeee-3fa6830aa1c2")
        self.assertEqual(contract.slot_id, "19a946fb-c4a6-40e1-88e7-6416feaa9626")

    def test_lossless_round_trip(self):
        self.assertEqual(Request.model_validate(INIT).dump(), INIT)
        self.assertEqual(Request.model_validate(CONFIRM).dump(), CONFIRM)

    def test_unknown_fields_and_types_survive(self):
        future = copy.deepcopy(CONFIRM)
        contract = future["message"]["contract"]
        contract["consideration"] = [{"id": "c1", "price": {"currency": "INR", "value": "500"}}]
        contract["participants"][0]["participantAttributes"]["preferredContact"] = "SMS"
        contract["commitments"][0]["commitmentAttributes"] = {
            "@context": "https://schema.beckn.io/HealthBilling/v2.1/context.jsonld",
            "@type": "hbl:HealthBilling",
            "payer": "SELF",
        }
        parsed = Request.model_validate(future)
        self.assertEqual(parsed.dump(), future)
        self.assertIs(type(parsed.message.contract.commitments[0].commitment_attributes), JsonLdModel)

    def test_attach_part_by_subclassing(self):
        future = copy.deepcopy(CONFIRM)
        future["message"]["contract"]["consideration"] = [{"id": "c1", "price": {"currency": "INR", "value": "500"}}]

        class Paid(BookingContract, HasConsideration):
            pass

        class PaidMessage(ContractMessage[Paid]):
            pass

        parsed = BecknRequest[PaidMessage].model_validate(future)
        self.assertEqual(parsed.message.contract.consideration[0].price.value, "500")
        self.assertIsNotNone(parsed.message.contract.patient)
        self.assertEqual(parsed.dump(), future)

    def test_registry_resolves_new_type_by_declaration(self):
        class HealthBilling(JsonLdModel):
            LD_TYPE = "hbl:HealthBillingRegistry"
            payer: str | None = None

        payload = copy.deepcopy(CONFIRM)
        payload["message"]["contract"]["commitments"][0]["commitmentAttributes"] = {
            "@type": "hbl:HealthBillingRegistry",
            "payer": "SELF",
        }
        attrs = Request.model_validate(payload).message.contract.commitments[0].commitment_attributes
        self.assertIsInstance(attrs, HealthBilling)
        self.assertEqual(attrs.payer, "SELF")

    def test_version_pinning_by_context(self):
        v22 = "https://schema.beckn.io/HealthParticipant/v2.2/context.jsonld"

        class HealthParticipantV22(HealthParticipant):
            LD_CONTEXT = v22
            LD_CONTEXTS = (v22,)
            preferred_contact: str | None = None

        payload = copy.deepcopy(INIT)
        payload["message"]["contract"]["participants"][0]["participantAttributes"]["@context"] = v22
        payload["message"]["contract"]["participants"][0]["participantAttributes"]["preferredContact"] = "SMS"
        pinned = Request.model_validate(payload).message.contract.patient.participant_attributes
        plain = Request.model_validate(INIT).message.contract.patient.participant_attributes
        self.assertIs(type(pinned), HealthParticipantV22)
        self.assertEqual(pinned.preferred_contact, "SMS")
        self.assertIs(type(plain), HealthParticipant)

    def test_naive_timestamp_rejected(self):
        bad = copy.deepcopy(INIT)
        bad["context"]["timestamp"] = "2026-06-19T16:06:00"
        with self.assertRaises(ValidationError):
            Request.model_validate(bad)

    def test_timestamp_serialised_like_care(self):
        self.assertEqual(Request.model_validate(INIT).dump()["context"]["timestamp"], INIT["context"]["timestamp"])


class AckTests(SimpleTestCase):
    def test_ack(self):
        self.assertEqual(ack(), {"message": {"ack": {"status": "ACK"}}})
        self.assertFalse(is_nack(ack()))

    def test_nack_wire_shape(self):
        body = nack("70001", "unsupported", "CONTEXT-ERROR")
        self.assertEqual(body["message"]["ack"]["status"], "NACK")
        self.assertEqual(body["error"], {"type": "CONTEXT-ERROR", "code": "70001", "message": "unsupported"})
        self.assertTrue(is_nack(body))

    def test_nack_from_validation_error(self):
        bad = copy.deepcopy(CONFIRM)
        bad["context"].pop("transactionId")
        with self.assertRaises(ValidationError) as caught:
            Request.model_validate(bad)
        body = nack_from(caught.exception)
        self.assertEqual(body["error"]["code"], "70002")
        self.assertIn("transactionId", body["error"]["message"])

    def test_is_nack_handles_non_dict_and_message_status(self):
        self.assertFalse(is_nack("oops"))
        self.assertTrue(is_nack({"message": {"status": "nack"}}))


class _Patient:
    name = "Meena Joshi"
    gender = "FEMALE"
    dob = date(1985, 4, 12)
    language = "en"
    abha = "91-1234-5678-9012"


class _Spec:
    resource_id = "7e772edf-4c5f-4707-aeee-3fa6830aa1c2"
    slot_id = "19a946fb-c4a6-40e1-88e7-6416feaa9626"
    window_start = datetime(2026, 6, 19, 16, 6, tzinfo=UTC)
    window_end = datetime(2026, 6, 19, 16, 30, tzinfo=UTC)
    service_type = "PHYSICAL_CONSULTATION"
    patient = _Patient


class StatusPart(Part):
    field = "status"

    def build(self, spec, builder):
        return Status(code=builder.status_code)


class CommitmentPart(Part):
    field = "commitments"

    def build(self, spec, builder):
        rid = spec.resource_id
        return [
            Commitment(
                id=f"commitment-{rid}",
                status=Status(descriptor=Descriptor(code=builder.status_code)),
                resources=[Resource(id=rid, quantity=Quantity(count=1))],
                offer=Offer(id=f"offer-{rid}", resource_ids=[rid]),
            )
        ]


class PatientPart(Part):
    field = "participants"

    def build(self, spec, builder):
        p = spec.patient
        return [
            Participant(
                descriptor=Descriptor(name=p.name, short_desc="Patient (Subject)"),
                participant_attributes=HealthParticipant(
                    participant_role=PATIENT,
                    primary_language=p.language,
                    gender=p.gender,
                    date_of_birth=p.dob,
                    health_ids=[HealthId(system="ABHA", value=p.abha)],
                ),
            )
        ]


class SlotPart(Part):
    field = "performance"

    def build(self, spec, builder):
        return [
            Performance(
                id=spec.slot_id,
                performance_attributes=HealthPerformance(
                    health_service_type=spec.service_type,
                    appointment_window_start=spec.window_start,
                    appointment_window_end=spec.window_end,
                ),
            )
        ]


class HealthContractPart(Part):
    field = "contract_attributes"

    def build(self, spec, builder):
        return HealthContract(health_service_type=spec.service_type)


class InitBuilder(ContractBuilder):
    contract_model = BookingContract
    parts = (StatusPart, CommitmentPart, PatientPart, SlotPart, HealthContractPart)


class BuilderTests(SimpleTestCase):
    def test_parts_reproduce_sample_contract(self):
        self.assertEqual(InitBuilder().build(_Spec).dump(), INIT["message"]["contract"])

    def test_appended_part_is_emitted_and_rest_unchanged(self):
        class ConsiderationPart(Part):
            field = "consideration"

            def build(self, spec, builder):
                return [Consideration(id="c1", price=Price(currency="INR", value="500"))]

        class Paid(BookingContract, HasConsideration):
            pass

        class PaidBuilder(InitBuilder):
            contract_model = Paid
            parts = (*InitBuilder.parts, ConsiderationPart)

        paid = PaidBuilder().build(_Spec).dump()
        self.assertEqual(paid["consideration"], [{"id": "c1", "price": {"currency": "INR", "value": "500"}}])
        self.assertEqual({k: v for k, v in paid.items() if k != "consideration"}, INIT["message"]["contract"])

    def test_part_returning_none_is_skipped(self):
        class NoneBuilder(ContractBuilder):
            class NonePart(Part):
                field = "status"

                def build(self, spec, builder):
                    return None

            parts = (NonePart,)

        self.assertEqual(NoneBuilder().build(object()).dump(), {})

    def test_echo_sets_status_and_keeps_unknown_fields(self):
        inbound = copy.deepcopy(INIT["message"])
        inbound["contract"]["somethingNew"] = {"a": 1}
        echoed = InitBuilder.echo(inbound, "DRAFT")
        self.assertEqual(echoed["contract"]["status"]["code"], "DRAFT")
        self.assertEqual(echoed["contract"]["somethingNew"], {"a": 1})
        self.assertEqual(inbound["contract"]["status"]["code"], INIT["message"]["contract"]["status"]["code"])

    def test_inbound_parses_contract(self):
        contract = InitBuilder.inbound(INIT["message"])
        self.assertEqual(contract.resource_id, "7e772edf-4c5f-4707-aeee-3fa6830aa1c2")


class BecknModelTests(SimpleTestCase):
    def test_snake_python_camel_wire(self):
        class Thing(BecknModel):
            short_desc: str | None = None

        self.assertEqual(Thing(short_desc="x").dump(), {"shortDesc": "x"})
        self.assertEqual(Thing.model_validate({"shortDesc": "y"}).short_desc, "y")
