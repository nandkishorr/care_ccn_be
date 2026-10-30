from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from care_ccn_be.exceptions import BecknError
from care_ccn_be.flows.appointment.flow import AppointmentFlow
from care_ccn_be.flows.registry import FlowRegistry
from care_ccn_be.models import BecknMessage, Delivery, Direction
from care_ccn_be.services.publisher import PublishSpec, build_publish_payload, publish_catalog
from care_ccn_be.tests.appointment.base import APPOINTMENT_PLUGIN, AppointmentTestBase
from care_ccn_be.tests.framework.base import BPP_ONIX

BOTH = {**APPOINTMENT_PLUGIN, "BECKN_ENABLED_FLOWS": ["appointment", "echo"]}


class PublisherTests(AppointmentTestBase):
    def test_payload_has_a_publish_context_catalogs_and_directives(self):
        payload = build_publish_payload(AppointmentFlow)
        context, message = payload["context"], payload["message"]
        self.assertEqual(context["action"], "catalog/publish")
        self.assertEqual((context["bppId"], context["networkId"]), ("bpp.care", "nfh-test"))
        self.assertNotIn("bapId", context)
        (catalog,) = message["catalogs"]
        self.assertEqual(catalog["id"], f"catalog-{self.facility.external_id}")
        self.assertEqual(catalog["bppId"], "bpp.care")
        self.assertEqual(
            message["publishDirectives"],
            [
                {
                    "catalogId": catalog["id"],
                    "catalogType": "REGULAR",
                    "updateMode": "FULL",
                    "visibleTo": ["nfh-test"],
                }
            ],
        )

    def test_dry_run_records_and_sends_nothing(self):
        self.stub_delivery()
        result = publish_catalog(AppointmentFlow, dry_run=True)
        self.assertTrue(result.dry_run)
        self.assertEqual(len(result.catalogs), 1)
        self.assertEqual(BecknMessage.objects.count(), 0)
        self.assertEqual(self.sent, [])

    def test_publishing_records_and_delivers_through_the_bpp_caller(self):
        self.stub_delivery()
        result = publish_catalog(AppointmentFlow)
        message = BecknMessage.objects.get()
        self.assertEqual(result.message, message)
        self.assertIsNone(message.transaction)
        self.assertEqual((message.action, message.direction), ("catalog/publish", Direction.OUT))
        self.assertEqual(message.delivery_status, Delivery.ACKED)
        base_url, action, payload = self.sent[0]
        self.assertEqual((base_url, action), (BPP_ONIX, "catalog/publish"))
        self.assertEqual(payload["message"]["catalogs"][0]["resources"][0]["id"], str(self.resource.external_id))

    def test_publishing_twice_sends_two_messages(self):
        self.stub_delivery()
        publish_catalog(AppointmentFlow)
        publish_catalog(AppointmentFlow)
        self.assertEqual(BecknMessage.objects.count(), 2)
        self.assertEqual(len({s[2]["context"]["messageId"] for s in self.sent}), 2)

    def test_without_a_caller_the_delivery_is_skipped(self):
        plugin = {**APPOINTMENT_PLUGIN, "BECKN_BPP_CALLER_URL": ""}
        with override_settings(PLUGIN_CONFIGS={"care_ccn_be": plugin}):
            publish_catalog(AppointmentFlow)
        self.assertEqual(BecknMessage.objects.get().delivery_status, Delivery.SKIPPED)

    def test_private_schedules_are_only_published_on_request(self):
        private = self.make_resource(self.create_user(username="dr.private"))
        self.make_schedule(private, is_public=False)
        public_ids = [
            r["id"] for c in build_publish_payload(AppointmentFlow)["message"]["catalogs"] for r in c["resources"]
        ]
        all_ids = [
            r["id"]
            for c in build_publish_payload(AppointmentFlow, public_only=False)["message"]["catalogs"]
            for r in c["resources"]
        ]
        self.assertEqual(public_ids, [str(self.resource.external_id)])
        self.assertEqual(set(all_ids), {str(self.resource.external_id), str(private.external_id)})

    def test_a_flow_without_a_catalog_cannot_be_published(self):
        class NoCatalog:
            name = "none"
            catalog_builder = None

        with self.assertRaises(BecknError) as caught:
            publish_catalog(NoCatalog)
        self.assertEqual(caught.exception.code, "70003")

    def test_spec_defaults_to_public_only(self):
        self.assertTrue(PublishSpec().public_only)


@override_settings(PLUGIN_CONFIGS={"care_ccn_be": BOTH})
class PublishCommandTests(AppointmentTestBase):
    def run_command(self, *args):
        out = StringIO()
        call_command("publish_beckn_catalog", *args, stdout=out)
        return out.getvalue()

    def test_every_enabled_flow_with_a_catalog_is_published(self):
        self.stub_delivery()
        output = self.run_command()
        self.assertIn("[appointment] queued 1 catalog(s)", output)
        self.assertIn("[echo] queued 1 catalog(s)", output)
        self.assertEqual(BecknMessage.objects.filter(action="catalog/publish").count(), 2)

    def test_a_single_flow(self):
        self.stub_delivery()
        self.run_command("--flow", "appointment")
        message = BecknMessage.objects.get()
        self.assertEqual(message.payload["message"]["catalogs"][0]["id"], f"catalog-{self.facility.external_id}")

    def test_dry_run_prints_the_payload_and_publishes_nothing(self):
        self.stub_delivery()
        output = self.run_command("--flow", "appointment", "--dry-run")
        self.assertIn('"catalog/publish"', output)
        self.assertIn("dry run: 1 catalog(s) built, not published", output)
        self.assertEqual(BecknMessage.objects.count(), 0)

    def test_all_includes_private_schedules(self):
        private = self.make_resource(self.create_user(username="dr.private"))
        self.make_schedule(private, is_public=False)
        output = self.run_command("--flow", "appointment", "--dry-run", "--all")
        self.assertIn(str(private.external_id), output)
        self.assertNotIn(str(private.external_id), self.run_command("--flow", "appointment", "--dry-run"))

    def test_an_unknown_flow(self):
        with self.assertRaisesMessage(CommandError, "unknown flow"):
            self.run_command("--flow", "nope")

    def test_a_disabled_flow(self):
        self.assertIsNotNone(FlowRegistry.get("referral"))
        with self.assertRaisesMessage(CommandError, "not enabled"):
            self.run_command("--flow", "referral")
        with self.assertRaisesMessage(CommandError, "not enabled"):
            self.run_command("--coordination")

    def test_nothing_to_publish(self):
        with override_settings(PLUGIN_CONFIGS={"care_ccn_be": {**BOTH, "BECKN_ENABLED_FLOWS": []}}):
            with self.assertRaisesMessage(CommandError, "no enabled flow publishes a catalog"):
                self.run_command()
