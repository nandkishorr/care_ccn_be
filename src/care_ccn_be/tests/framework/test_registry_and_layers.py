import ast
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase, override_settings

import care_ccn_be
from care_ccn_be.api.viewsets.lookup import VIEWSETS, get_viewset
from care_ccn_be.flows.lookup import enabled_flows, get_flow, is_flow_enabled
from care_ccn_be.tests.framework import echo_flow  # noqa: F401  (registers EchoFlow)
from care_ccn_be.tests.framework.base import PLUGIN

ROOT = Path(care_ccn_be.__file__).parent


class BapOnlyViewSet:
    adapter = None
    adapters = ("bap",)


class LookupTests(SimpleTestCase):
    def test_enabled_follows_the_setting(self):
        with override_settings(PLUGIN_CONFIGS={"care_ccn_be": {**PLUGIN, "BECKN_ENABLED_FLOWS": ["echo"]}}):
            self.assertEqual([f.name for f in enabled_flows()], ["echo"])
            self.assertTrue(is_flow_enabled("echoing"))
        with override_settings(PLUGIN_CONFIGS={"care_ccn_be": {**PLUGIN, "BECKN_ENABLED_FLOWS": []}}):
            self.assertEqual(enabled_flows(), [])
            self.assertFalse(is_flow_enabled("echo"))

    def test_aliases_resolve_to_the_canonical_flow(self):
        self.assertIs(get_flow("echoing"), echo_flow.EchoFlow)
        with self.assertRaises(KeyError):
            get_flow("unknown")

    def test_lists_the_plugin_flows(self):
        from care_ccn_be.flows.appointment import AppointmentFlow
        from care_ccn_be.flows.referral import ReferralFlow

        self.assertIs(get_flow("appointment"), AppointmentFlow)
        self.assertIs(get_flow("referral"), ReferralFlow)

    def test_ready_registers_tasks_and_receivers_of_every_flow(self):
        from celery import current_app
        from django.db.models.signals import post_save

        for task in ("care_ccn_be.appointment.notify", "care_ccn_be.referral.submit", "care_ccn_be.referral.complete"):
            self.assertIn(task, current_app.tasks)
        uids = {receiver[0][0] for receiver in post_save.receivers}
        self.assertTrue({"care_ccn_be.appointment.booking_post_save", "care_ccn_be.referral.referral_saved"} <= uids)

    def test_viewset_respects_the_viewset_adapters(self):
        with mock.patch.dict(VIEWSETS, {"echo": BapOnlyViewSet}):
            self.assertIs(get_viewset("bap", "echoing"), BapOnlyViewSet)
            self.assertIsNone(get_viewset("bpp", "echo"))
        self.assertIsNone(get_viewset("bap", "unknown"))

    def test_relocated_models_keep_their_database_identity(self):
        from care_ccn_be.models import AppointmentLink, ReferralLink
        from care_ccn_be.models.appointment import AppointmentLink as MovedAppointmentLink
        from care_ccn_be.models.referral import ReferralLink as MovedReferralLink

        for model, moved, name in (
            (AppointmentLink, MovedAppointmentLink, "appointmentlink"),
            (ReferralLink, MovedReferralLink, "referrallink"),
        ):
            self.assertIs(model, moved)
            self.assertEqual(model._meta.label_lower, f"care_ccn_be.{name}")
            self.assertEqual(model._meta.db_table, f"care_ccn_be_{name}")

    def test_relocated_tasks_keep_their_celery_names(self):
        from care_ccn_be.tasks import deliver_beckn_message
        from care_ccn_be.tasks.appointment import send_appointment_update
        from care_ccn_be.tasks.referral import complete_referral, submit_referral

        for task, name in (
            (deliver_beckn_message, "care_ccn_be.deliver_beckn_message"),
            (send_appointment_update, "care_ccn_be.appointment.notify"),
            (submit_referral, "care_ccn_be.referral.submit"),
            (complete_referral, "care_ccn_be.referral.complete"),
        ):
            self.assertEqual(task.name, name)

    def test_flow_adapters_share_one_viewset(self):
        from care_ccn_be.api.viewsets.appointment import AppointmentViewSet
        from care_ccn_be.api.viewsets.referral import ReferralViewSet

        for flow, viewset in (
            ("appointment", AppointmentViewSet),
            ("referral", ReferralViewSet),
        ):
            self.assertIs(get_viewset("bap", flow), viewset)
            self.assertIs(get_viewset("bpp", flow), viewset)
            self.assertEqual(viewset.__module__, f"care_ccn_be.api.viewsets.{flow}")
            self.assertEqual(viewset(adapter="bap").adapter, "bap")
            self.assertEqual(viewset(adapter="bpp").adapter, "bpp")
            self.assertIsNone(viewset.adapter)

    def test_shared_viewset_routes_keep_adapter_actions_separate(self):
        from care_ccn_be.api.viewsets.appointment import AppointmentViewSet
        from care_ccn_be.urls import AdapterRouter

        for adapter in ("bap", "bpp"):
            router = AdapterRouter(adapter)
            router.register(f"{adapter}/appointment", AppointmentViewSet, basename=f"{adapter}-appointment")
            callbacks = [pattern.callback for pattern in router.urls]
            actions = {name for callback in callbacks for name in callback.actions.values()}
            self.assertTrue({"list", "retrieve"} <= actions)
            if adapter == "bap":
                self.assertTrue({"discover", "select", "confirm", "update_contract", "cancel"} <= actions)
            else:
                self.assertEqual(actions, {"list", "retrieve"})
            for callback in callbacks:
                self.assertIs(callback.cls, AppointmentViewSet)
                self.assertEqual(callback.initkwargs["adapter"], adapter)

    def test_shared_viewset_queries_are_scoped_to_the_bound_adapter(self):
        from care_ccn_be.api.viewsets.appointment import AppointmentViewSet

        with mock.patch("care_ccn_be.api.viewsets.base.BecknTransaction.objects.filter") as filter_transactions:
            for adapter in ("bap", "bpp"):
                AppointmentViewSet(adapter=adapter).get_queryset()
                filter_transactions.assert_called_with(adapter=adapter, flow="appointment")


class LayeringTests(SimpleTestCase):
    @staticmethod
    def imports(path: Path) -> set[str]:
        found = set()
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                found.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                found.add(node.module)
        return found

    def test_flows_never_import_each_other(self):
        flow_names = {p.name for p in (ROOT / "flows").iterdir() if p.is_dir() and not p.name.startswith("_")}
        for parent in ("flows", "resources", "resources/builders", "resources/catalog"):
            for name in flow_names:
                pkg = ROOT / parent / name
                paths = [pkg.with_suffix(".py")] if pkg.with_suffix(".py").is_file() else pkg.rglob("*.py")
                for path in paths:
                    for module in self.imports(path):
                        for other in flow_names - {name}:
                            for prefix in (
                                f"care_ccn_be.flows.{other}",
                                f"care_ccn_be.resources.{other}",
                                f"care_ccn_be.resources.builders.{other}",
                                f"care_ccn_be.resources.catalog.{other}",
                            ):
                                self.assertFalse(module.startswith(prefix), f"{path} imports flow {other}")

    def test_beckn_resources_stay_pure(self):
        for path in (ROOT / "resources" / "beckn").rglob("*.py"):
            for module in self.imports(path):
                self.assertFalse(module.startswith("django"), f"{path} imports {module}")
                if module.startswith("care_ccn_be"):
                    self.assertTrue(module.startswith("care_ccn_be.resources.beckn"), f"{path} imports {module}")

    def test_only_the_store_writes_transactions_and_messages(self):
        writers = ("BecknTransaction.objects.create", "BecknMessage.objects.create", "get_or_create")
        for path in ROOT.rglob("*.py"):
            rel = path.relative_to(ROOT)
            if rel.parts[0] == "tests" or rel.name in {"store.py", "0001_initial.py"} or rel.parts[0] == "migrations":
                continue
            text = path.read_text()
            for needle in writers[:2]:
                self.assertNotIn(needle, text, f"{rel} writes through the ORM instead of the store")

    def test_no_flow_or_action_branching_in_the_framework(self):
        for sub in ("api/bap", "api/bpp"):
            for path in (ROOT / sub).rglob("*.py"):
                text = path.read_text()
                self.assertNotIn("if flow ==", text)
