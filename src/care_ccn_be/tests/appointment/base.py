import datetime

from care.emr.models import Availability, SchedulableResource, Schedule
from care.emr.models.scheduling.booking import TokenBooking, TokenSlot
from care.emr.resources.scheduling.schedule.spec import SchedulableResourceTypeOptions, SlotTypeOptions
from django.test import override_settings
from django.test.utils import ignore_warnings
from django.utils import timezone

from care_ccn_be.flows.appointment.models import AppointmentLink
from care_ccn_be.tests.framework.base import PLUGIN, FrameworkTestBase

APPOINTMENT_PLUGIN = {**PLUGIN, "BECKN_ENABLED_FLOWS": ["appointment"], "BECKN_NETWORK_ID": "nfh-test"}
BPP = {"bppId": "bpp.care", "bppUri": "http://bpp.care"}


@ignore_warnings(category=RuntimeWarning, message=r".*received a naive datetime.*")
@override_settings(
    ROOT_URLCONF="care_ccn_be.tests.appointment.urls", PLUGIN_CONFIGS={"care_ccn_be": APPOINTMENT_PLUGIN}
)
class AppointmentTestBase(FrameworkTestBase):
    """One public practitioner schedule with half-hour slots (09:00-11:00) on a day three days from now."""

    def setUp(self):
        super().setUp()
        self.system = self.create_user(username="beckn_system")
        self.doctor = self.create_user(first_name="Anita", last_name="Rao", username="dr.anita")
        self.facility = self.create_facility(self.user, name="City Clinic")
        self.day = timezone.localdate() + datetime.timedelta(days=3)
        self.resource = self.make_resource(self.doctor)
        self.schedule = self.make_schedule(self.resource)
        self.patient = self.create_patient(
            name="Meena Joshi", gender="female", date_of_birth=datetime.date(1985, 4, 12), phone_number="9876543210"
        )

    def make_resource(self, user):
        return SchedulableResource.objects.create(
            resource_type=SchedulableResourceTypeOptions.practitioner.value, user=user, facility=self.facility
        )

    def make_schedule(self, resource, is_public=True, meta=None, tokens_per_slot=1, name="Morning OPD"):
        schedule = Schedule.objects.create(
            resource=resource,
            name=name,
            valid_from=timezone.now() - datetime.timedelta(days=1),
            valid_to=timezone.now() + datetime.timedelta(days=30),
            is_public=is_public,
            meta=meta or {},
        )
        Availability.objects.create(
            schedule=schedule,
            name="Morning",
            slot_type=SlotTypeOptions.appointment.value,
            slot_size_in_minutes=30,
            tokens_per_slot=tokens_per_slot,
            availability=[{"day_of_week": self.day.weekday(), "start_time": "09:00:00", "end_time": "11:00:00"}],
        )
        return schedule

    def slots(self, resource=None, day=None):
        from care_ccn_be.services import scheduling

        return scheduling.list_slots_for_day(resource or self.resource, day or self.day)

    def first_slot(self, resource=None):
        return self.slots(resource)[0]

    def bap(self, txn_id=None, action="discover", body=None, client=None):
        url = "/bap/appointment/discover/" if action == "discover" else f"/bap/appointment/{txn_id}/{action}/"
        return (client or self.client).post(url, body or {}, format="json")

    def poll(self, txn_id, action=None):
        suffix = f"?action={action}" if action else ""
        return self.client.get(f"/bap/appointment/{txn_id}/{suffix}").json()

    def request(self, action, txn_id, message, message_id="m-1", **ctx):
        from care_ccn_be.tests.framework.base import inbound_request

        payload = inbound_request(
            action, txn_id, message_id, network="nfh-test", message=message, bppId="bpp.care", **ctx
        )
        return self.anon.post(f"/bpp/receiver/{action}", payload, format="json")

    def contract(self, resource=None, slot=None, patient=True, **attributes):
        """An inbound appointment contract the way a BAP would send it."""
        resource = resource or self.resource
        contract = {
            "status": {"code": "DRAFT"},
            "commitments": [
                {
                    "id": "commitment-1",
                    "resources": [{"id": str(resource.external_id), "quantity": {"count": 1}}],
                    "offer": {"id": f"offer-{resource.external_id}", "resourceIds": [str(resource.external_id)]},
                }
            ],
            "contractAttributes": {
                "@context": "https://schema.beckn.io/HealthContract/v2.1/context.jsonld",
                "@type": "hct:HealthContract",
                "healthServiceType": "PHYSICAL_CONSULTATION",
                **attributes,
            },
        }
        if slot is not None:
            contract["performance"] = [{"id": str(slot.external_id)}]
        if patient:
            contract["participants"] = [
                {
                    "descriptor": {"name": "Meena Joshi"},
                    "participantAttributes": {
                        "@type": "hpa:HealthParticipant",
                        "participantRole": "PATIENT",
                        "gender": "FEMALE",
                        "dateOfBirth": "1985-04-12",
                        "healthIds": [{"system": "ABHA", "value": "91-1234-5678-9012"}],
                    },
                }
            ]
        return {"contract": contract}

    @staticmethod
    def booking_of(txn):
        link = AppointmentLink.objects.filter(transaction=txn, active=True).select_related("token_booking").first()
        return link.token_booking if link else None

    @staticmethod
    def booking_count():
        return TokenBooking.objects.count()

    @staticmethod
    def slot_count():
        return TokenSlot.objects.count()
