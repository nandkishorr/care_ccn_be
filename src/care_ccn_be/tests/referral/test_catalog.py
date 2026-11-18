from io import StringIO

from care.emr.models.organization import FacilityOrganization
from django.core.management import call_command
from django.test import override_settings
from model_bakery import baker

from care_ccn_be.flows.referral import ReferralFlow
from care_ccn_be.models import BecknMessage
from care_ccn_be.services.publisher import build_publish_payload
from care_ccn_be.tests.referral.base import REFERRAL_PLUGIN, ReferralTestBase


class ReferralCatalogTests(ReferralTestBase):
    def catalogs(self, public_only=True):
        return build_publish_payload(ReferralFlow, public_only)["message"]["catalogs"]

    def test_one_catalog_per_facility_with_a_public_active_availability(self):
        organization = baker.make(FacilityOrganization, facility=self.target, name="Cardiology")
        unit = self.receiving_availability(
            None, "lab", entity_type="internal_organization", internal_organization=organization, name="Lab unit"
        )
        self.receiving_availability(self.target, "lab", is_public=False)
        self.receiving_availability(self.target, "lab", status="inactive")
        self.receiving_availability(self.target, "lab", deleted=True)
        (catalog,) = self.catalogs()
        facility_id = self.target.external_id
        self.assertEqual(catalog["id"], f"catalog-coordinator-{facility_id}")
        self.assertEqual(catalog["descriptor"], {"name": "Care Coordination Desk"})
        self.assertEqual(catalog["provider"]["id"], str(facility_id))
        self.assertEqual((catalog["bppId"], catalog["bppUri"]), ("bpp.care", "http://bpp.care"))
        self.assertEqual(
            [resource["id"] for resource in catalog["resources"]],
            [f"coordinator-{facility_id}-resource", str(self.receiving.external_id), str(unit.external_id)],
        )
        self.assertEqual(
            [offer["id"] for offer in catalog["offers"]],
            [f"coordinator-{facility_id}-offer", f"offer-{self.receiving.external_id}", f"offer-{unit.external_id}"],
        )
        self.assertEqual(catalog["offers"][2]["resourceIds"], [str(unit.external_id)])
        attributes = catalog["resources"][2]["resourceAttributes"]
        self.assertEqual(attributes["@type"], "scres:ServiceCoordinationResource")
        self.assertEqual(attributes["acceptanceMode"], "MANUAL_REVIEW")
        scope = attributes["coordinationScope"]
        self.assertEqual(scope["referralCategories"][0]["code"], "CLINICAL")
        self.assertEqual([item["code"] for item in scope["targetServiceTypes"]], ["INVESTIGATION"])
        self.assertEqual([item["code"] for item in scope["programmeScope"]], ["PMJAY"])
        self.assertEqual(attributes["supportedUrgencyTiers"], ["ROUTINE", "URGENT", "EMERGENCY"])
        self.assertEqual(
            attributes["referralAvailability"],
            {
                "id": str(unit.external_id),
                "referralType": "lab",
                "entityType": "internal_organization",
                "organizationId": str(organization.external_id),
                "organizationName": "Cardiology",
            },
        )

    def test_the_coordination_desk_leads_the_catalog(self):
        (catalog,) = self.catalogs()
        facility_id = self.target.external_id
        desk, offer = catalog["resources"][0], catalog["offers"][0]
        self.assertEqual(desk["descriptor"], {"name": "Care Coordination Desk Coordination"})
        attributes = desk["resourceAttributes"]
        self.assertNotIn("referralAvailability", attributes)
        self.assertEqual(
            [item["code"] for item in attributes["coordinationScope"]["targetServiceTypes"]],
            ["CONSULTATION", "INVESTIGATION", "DISPENSING"],
        )
        self.assertEqual(offer["resourceIds"], [f"coordinator-{facility_id}-resource"])
        self.assertEqual(
            offer["offerAttributes"],
            {
                "@context": "https://schema.beckn.io/ServiceCoordinationOffer/v2.1/context.jsonld",
                "@type": "scoff:ServiceCoordinationOffer",
                "coordinationSlaWindows": [
                    {"clinicalUrgencyTier": "ROUTINE", "lapseWindowHours": 168, "slaBreachWindowHours": 168},
                    {"clinicalUrgencyTier": "URGENT", "lapseWindowHours": 48, "slaBreachWindowHours": 48},
                    {"clinicalUrgencyTier": "EMERGENCY", "lapseWindowHours": 1, "slaBreachWindowHours": 1},
                ],
                "retryPolicy": "ONE_SILENT_RETRY",
                "handoffSupported": True,
                "maxActiveReferrals": 500,
            },
        )
        self.assertEqual(catalog["offers"][1]["offerAttributes"], offer["offerAttributes"])

    def test_a_facility_scoped_catalog_keeps_the_desk_without_availabilities(self):
        payload = build_publish_payload(ReferralFlow, facility_ids=[self.origin.pk], update_mode="MERGE")
        (catalog,) = payload["message"]["catalogs"]
        self.assertEqual(catalog["id"], f"catalog-coordinator-{self.origin.external_id}")
        desk = f"coordinator-{self.origin.external_id}-resource"
        self.assertEqual([resource["id"] for resource in catalog["resources"]], [desk])
        (directive,) = payload["message"]["publishDirectives"]
        self.assertEqual((directive["catalogId"], directive["updateMode"]), (catalog["id"], "MERGE"))

    def test_forms_are_not_published(self):
        self.map_questionnaire(self.receiving, self.referral_questionnaire())
        self.assertNotIn("Emergency Care", str(self.catalogs()))

    def test_facilities_without_an_availability_are_left_out(self):
        self.receiving.status = "inactive"
        self.receiving.save()
        self.assertEqual(self.catalogs(), [])
        self.receiving_availability(self.origin, is_public=False)
        self.assertEqual(self.catalogs(), [])
        (catalog,) = self.catalogs(public_only=False)
        self.assertEqual(catalog["provider"]["id"], str(self.origin.external_id))

    def test_the_command_publishes_the_referral_catalog(self):
        self.stub_delivery()
        out = StringIO()
        call_command("publish_beckn_catalog", "--flow", "referral", stdout=out)
        self.assertIn("[referral] queued 1 catalog(s)", out.getvalue())
        message = BecknMessage.objects.get(action="catalog/publish")
        self.assertEqual(
            message.payload["message"]["catalogs"][0]["resources"][1]["id"], str(self.receiving.external_id)
        )
        self.assertEqual(message.payload["message"]["publishDirectives"][0]["updateMode"], "FULL")


AUTO_PUBLISH = {"care_ccn_be": {**REFERRAL_PLUGIN, "BECKN_REFERRAL_CATALOG_AUTO_PUBLISH": True}}


class CatalogRepublishTests(ReferralTestBase):
    def setUp(self):
        super().setUp()
        self.stub_delivery()

    def published(self):
        return BecknMessage.objects.filter(action="catalog/publish")

    def catalog_of(self, message):
        (catalog,) = message.payload["message"]["catalogs"]
        (directive,) = message.payload["message"]["publishDirectives"]
        self.assertEqual(directive["catalogId"], catalog["id"])
        return catalog, directive["updateMode"]

    def test_a_new_availability_is_merged_into_its_facility_catalog(self):
        self.receiving_availability(self.origin, "lab", is_public=False)
        with override_settings(PLUGIN_CONFIGS=AUTO_PUBLISH):
            first = self.receiving_availability(self.target, "lab", name="Lab desk")
        (message,) = self.published()
        catalog, mode = self.catalog_of(message)
        self.assertEqual(mode, "MERGE")
        self.assertEqual(catalog["id"], f"catalog-coordinator-{self.target.external_id}")
        desk = f"coordinator-{self.target.external_id}-resource"
        self.assertEqual(
            [resource["id"] for resource in catalog["resources"]],
            [desk, str(self.receiving.external_id), str(first.external_id)],
        )
        with override_settings(PLUGIN_CONFIGS=AUTO_PUBLISH):
            second = self.receiving_availability(self.target, "lab", name="Radiology desk")
        catalog, mode = self.catalog_of(self.published().latest("pk"))
        self.assertEqual(mode, "MERGE")
        self.assertEqual(catalog["id"], f"catalog-coordinator-{self.target.external_id}")
        self.assertEqual(catalog["resources"][-1]["id"], str(second.external_id))
        self.assertEqual(len(catalog["resources"]), 4)

    def test_an_updated_availability_is_merged_in_place(self):
        with override_settings(PLUGIN_CONFIGS=AUTO_PUBLISH):
            self.receiving.name = "Cardiology referrals"
            self.receiving.save()
        catalog, mode = self.catalog_of(self.published().get())
        self.assertEqual(mode, "MERGE")
        self.assertEqual(catalog["resources"][1]["descriptor"]["name"], "Cardiology referrals")

    def test_a_withdrawn_availability_replaces_its_facility_catalog(self):
        with override_settings(PLUGIN_CONFIGS=AUTO_PUBLISH):
            self.receiving.is_public = False
            self.receiving.save()
        catalog, mode = self.catalog_of(self.published().get())
        self.assertEqual(mode, "FULL")
        self.assertEqual(catalog["id"], f"catalog-coordinator-{self.target.external_id}")
        self.assertEqual(
            [resource["id"] for resource in catalog["resources"]], [f"coordinator-{self.target.external_id}-resource"]
        )

    def test_network_availabilities_do_not_republish(self):
        with override_settings(PLUGIN_CONFIGS=AUTO_PUBLISH):
            self.network_availability()
        self.assertFalse(self.published().exists())

    def test_turned_off_it_does_not_republish(self):
        self.receiving.save()
        self.assertFalse(self.published().exists())
