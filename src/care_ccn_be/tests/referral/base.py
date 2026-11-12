from care.emr.models.referral import Referral
from django.test import override_settings
from model_bakery import baker

from care_ccn_be.context_vars import suppress_beckn_signals
from care_ccn_be.models import Adapter
from care_ccn_be.services import availability as availabilities
from care_ccn_be.tests.framework.base import PLUGIN, FrameworkTestBase, inbound_callback, inbound_request

CC = {"BECKN_CC_BPP_ID": "cc.bpp", "BECKN_CC_BPP_URI": "http://cc.bpp"}
REFERRAL_PLUGIN = {**PLUGIN, **CC, "BECKN_ENABLED_FLOWS": ["referral"]}


@override_settings(ROOT_URLCONF="care_ccn_be.tests.referral.urls", PLUGIN_CONFIGS={"care_ccn_be": REFERRAL_PLUGIN})
class ReferralTestBase(FrameworkTestBase):
    def setUp(self):
        super().setUp()
        self.system = self.create_user(username="beckn_system")
        self.origin = self.create_facility(self.user)
        self.target = self.create_facility(self.user)

    def bap(self, txn_id=None, action="discover", body=None, client=None):
        url = "/bap/referral/discover/" if action == "discover" else f"/bap/referral/{txn_id}/{action}/"
        return (client or self.client).post(url, body or {}, format="json")

    def poll(self, txn_id):
        return self.client.get(f"/bap/referral/{txn_id}/").json()

    def message(self, txn_id, action):
        """The latest stored payload of ``action`` on the BAP transaction (``bap/message/``)."""
        results = self.client.get("/bap/message/", {"transaction_id": txn_id, "action": action}).json()["results"]
        return results[-1]["payload"]

    def new_transaction(self):
        self.stub_delivery()
        txn_id = self.bap().json()["transaction_id"]
        self.on_discover(txn_id)
        return txn_id

    def on_discover(self, txn_id, **ctx):
        payload = inbound_callback("on_discover", txn_id, message={"catalogs": [{"id": "c"}]}, **ctx)
        return self.anon.post("/bap/receiver/on_discover", payload, format="json")

    def callback(self, action, txn_id, message, message_id="cb-1", error=None, **ctx):
        payload = inbound_callback(action, txn_id, message_id=message_id, message=message, **ctx)
        if error:
            payload["error"] = error
        return self.anon.post(f"/bap/receiver/{action}", payload, format="json")

    def request(self, action, txn_id, message, message_id="m-1", **ctx):
        payload = inbound_request(action, txn_id, message_id, message=message, **ctx)
        return self.anon.post(f"/bpp/receiver/{action}", payload, format="json")

    def network_availability(self, referral_type="consultation", assigned=None):
        return availabilities.network(
            "cc.bpp", "http://cc.bpp", referral_type, str(assigned.external_id) if assigned else None
        )

    def create_referral(self, **kwargs):
        """A pending referral on a network availability, saved with signals suppressed (a user's would not be)."""
        defaults = {
            "availability": self.network_availability(),
            "patient": self.create_patient(name="Asha", gender="female"),
            "status": "pending",
            "title": "Heart",
            "note": "",
            "assignees": [],
        }
        with suppress_beckn_signals():
            return baker.make(Referral, **{**defaults, **kwargs})

    def referral_message(self, coordination_id="coord-1", facility=None, assigned=None, patient=None, **extra):
        participants = []
        if patient is not False:
            participants = [
                {
                    "id": "participant-patient-1",
                    "descriptor": {"name": "Asha Rao"},
                    "participantAttributes": {
                        "@type": "hpa:HealthParticipant",
                        "participantRole": "PATIENT",
                        "gender": "FEMALE",
                        "healthIds": [{"system": "ABHA", "value": "91-1111-2222-3333"}],
                        **(patient or {}),
                    },
                }
            ]
        attributes = {
            "@context": "https://schema.beckn.io/HealthReferral/v2.1/context.jsonld",
            "@type": "hrf:HealthReferral",
            "coordinationId": coordination_id,
            "lifecycleState": "ACTIVE",
            "clinicalUrgencyTier": "URGENT",
            **extra,
        }
        if facility is not None:
            attributes["facilityId"] = str(facility.external_id)
        if assigned is not None:
            attributes["assignedFacilityId"] = str(assigned.external_id)
        return {
            "contract": {
                "descriptor": {"name": "Cardiology referral", "shortDesc": "chest pain"},
                "status": {"code": "DRAFT"},
                "participants": participants,
                "contractAttributes": attributes,
            }
        }

    def bpp_txn_of(self, coordination_id):
        from care_ccn_be.models import BecknTransaction

        return BecknTransaction.objects.get(transaction_id=coordination_id, adapter=Adapter.BPP)
