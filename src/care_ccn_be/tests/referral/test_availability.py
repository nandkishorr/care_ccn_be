from io import StringIO

from care.emr.models.referral import ReferralAvailability
from care.security.authorization import AuthorizationController
from django.contrib.auth.models import AnonymousUser
from django.core.management import call_command
from django.core.management.base import CommandError

from care_ccn_be.resources.builders.referral import referral_type_for_category
from care_ccn_be.services import availability as availabilities
from care_ccn_be.tests.referral.base import ReferralTestBase


class AvailabilityServiceTests(ReferralTestBase):
    def test_network_availability_is_public_active_and_reused(self):
        first = availabilities.network("bpp.a", "http://a", "lab", "fac-1")
        again = availabilities.network("bpp.a", "http://a", "lab", "fac-1")
        self.assertEqual(first.pk, again.pk)
        self.assertEqual((first.entity_type, first.status, first.is_public), ("external", "active", True))
        self.assertEqual(
            availabilities.target_of(first), {"bppId": "bpp.a", "bppUri": "http://a", "assignedFacilityId": "fac-1"}
        )
        self.assertEqual(first.created_by, self.system)

    def test_targets_and_types_get_their_own_availability(self):
        base = availabilities.network("bpp.a", "http://a")
        self.assertNotEqual(base.pk, availabilities.network("bpp.b", "http://b").pk)
        self.assertNotEqual(base.pk, availabilities.network("bpp.a", "http://a", "lab").pk)
        self.assertNotEqual(base.pk, availabilities.network("bpp.a", "http://a", assigned_facility_id="f").pk)

    def test_only_network_availabilities_have_a_target(self):
        facility = availabilities.for_facility(self.target)
        self.assertEqual(availabilities.target_of(facility), {})
        self.assertFalse(availabilities.is_network(facility))
        self.assertTrue(availabilities.is_network(availabilities.network("bpp.a", "http://a")))

    def test_the_facility_availability_the_facility_configured_is_reused(self):
        configured = availabilities.for_facility(self.target)
        self.assertEqual(availabilities.for_facility(self.target).pk, configured.pk)
        self.assertEqual(
            (configured.entity_type, configured.facility, configured.is_public), ("facility", self.target, False)
        )
        self.assertNotEqual(availabilities.for_facility(self.target, "lab").pk, configured.pk)
        self.assertNotEqual(availabilities.for_facility(self.origin).pk, configured.pk)

    def test_an_inactive_facility_availability_is_not_reused(self):
        configured = availabilities.for_facility(self.target)
        ReferralAvailability.objects.filter(pk=configured.pk).update(status="inactive")
        self.assertNotEqual(availabilities.for_facility(self.target).pk, configured.pk)

    def test_assignees_are_a_copy_of_the_care_team(self):
        availability = availabilities.for_facility(self.target)
        availability.care_team = [{"user_id": 1, "role": {"code": "doctor"}}]
        copied = availabilities.assignees(availability)
        copied[0]["user_id"] = 2
        self.assertEqual(availability.care_team[0]["user_id"], 1)

    def test_categories_map_to_referral_types(self):
        self.assertEqual(referral_type_for_category("patient_care"), "consultation")
        for category in ("other", "medicines", None):
            self.assertEqual(referral_type_for_category(category), "lab")


class NetworkAvailabilityAccessTests(ReferralTestBase):
    def call(self, item, *args):
        return AuthorizationController.call(item, *args)

    def test_network_availabilities_are_viewable_by_any_signed_in_user(self):
        availability = availabilities.network("bpp.a", "http://a")
        self.assertTrue(self.call("can_view_referral_availability", self.user, availability))
        self.assertFalse(self.call("can_view_referral_availability", AnonymousUser(), availability))

    def test_inactive_network_availabilities_are_hidden(self):
        availability = availabilities.network("bpp.a", "http://a")
        availability.status = "inactive"
        self.assertFalse(self.call("can_view_referral_availability", self.user, availability))

    def test_external_availabilities_are_listed_for_signed_in_users(self):
        availabilities.network("bpp.a", "http://a")
        availabilities.for_facility(self.target)
        queryset = ReferralAvailability.objects.all()
        listed = self.call("get_accessible_referral_availabilities", queryset, self.user, "external")
        self.assertEqual([a.entity_type for a in listed], ["external"])
        self.assertFalse(
            self.call("get_accessible_referral_availabilities", queryset, AnonymousUser(), "external").exists()
        )

    def test_facility_availabilities_keep_the_core_rules(self):
        availability = availabilities.for_facility(self.target)
        self.assertFalse(self.call("can_view_referral_availability", self.user, availability))

    def test_a_user_can_submit_a_referral_on_a_network_availability(self):
        availability = availabilities.network("bpp.a", "http://a")
        self.assertTrue(self.call("can_submit_referral_availability", self.user, availability))


class AvailabilityCommandTests(ReferralTestBase):
    def run_command(self, *args):
        out = StringIO()
        call_command("beckn_referral_availability", *args, stdout=out)
        return out.getvalue()

    def test_defaults_to_the_coordination_centre_for_every_type(self):
        output = self.run_command()
        self.assertIn("consultation:", output)
        self.assertIn("lab:", output)
        self.assertEqual(ReferralAvailability.objects.filter(entity_type="external").count(), 2)
        self.run_command()
        self.assertEqual(ReferralAvailability.objects.filter(entity_type="external").count(), 2)
        availability = ReferralAvailability.objects.get(referral_type="lab")
        self.assertEqual(availabilities.target_of(availability), {"bppId": "cc.bpp", "bppUri": "http://cc.bpp"})

    def test_explicit_target_and_type(self):
        self.run_command("--bpp-id", "x", "--bpp-uri", "http://x", "--type", "lab", "--assigned-facility", "f-1")
        availability = ReferralAvailability.objects.get()
        self.assertEqual(
            availabilities.target_of(availability),
            {"bppId": "x", "bppUri": "http://x", "assignedFacilityId": "f-1"},
        )

    def test_a_target_is_required(self):
        from django.test import override_settings

        from care_ccn_be.tests.referral.base import REFERRAL_PLUGIN

        with override_settings(
            PLUGIN_CONFIGS={"care_ccn_be": {**REFERRAL_PLUGIN, "BECKN_CC_BPP_ID": "", "BECKN_CC_BPP_URI": ""}}
        ):
            with self.assertRaises(CommandError):
                self.run_command()
