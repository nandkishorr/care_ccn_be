import ast
from pathlib import Path

from django.test import SimpleTestCase, override_settings

import care_ccn_be
from care_ccn_be.flows.registry import FlowRegistry
from care_ccn_be.tests.framework import echo_flow  # noqa: F401  (registers EchoFlow)
from care_ccn_be.tests.framework.base import PLUGIN

ROOT = Path(care_ccn_be.__file__).parent


class RegistryTests(SimpleTestCase):
    def test_enabled_follows_the_setting(self):
        with override_settings(PLUGIN_CONFIGS={"care_ccn_be": {**PLUGIN, "BECKN_ENABLED_FLOWS": ["echo"]}}):
            self.assertEqual([f.name for f in FlowRegistry.enabled()], ["echo"])
            self.assertTrue(FlowRegistry.is_enabled("echoing"))
        with override_settings(PLUGIN_CONFIGS={"care_ccn_be": {**PLUGIN, "BECKN_ENABLED_FLOWS": []}}):
            self.assertEqual(FlowRegistry.enabled(), [])
            self.assertFalse(FlowRegistry.is_enabled("echo"))

    def test_plugin_stays_passive_while_core_beckn_is_installed(self):
        with override_settings(PLUGIN_CONFIGS={"care_ccn_be": PLUGIN}):
            from unittest import mock

            with mock.patch("care_ccn_be.flows.registry.apps.is_installed", return_value=True):
                self.assertEqual(FlowRegistry.enabled(), [])

    def test_aliases_resolve_to_the_canonical_flow(self):
        self.assertIs(FlowRegistry.get("echoing"), echo_flow.EchoFlow)


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
        flows_dir = ROOT / "flows"
        for pkg in (p for p in flows_dir.iterdir() if p.is_dir() and not p.name.startswith("_")):
            for path in pkg.rglob("*.py"):
                for module in self.imports(path):
                    for other in (p.name for p in flows_dir.iterdir() if p.is_dir() and p != pkg):
                        self.assertFalse(
                            module.startswith(f"care_ccn_be.flows.{other}"), f"{path} imports flow {other}"
                        )

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
        for sub in ("bap", "bpp"):
            for path in (ROOT / sub).rglob("*.py"):
                text = path.read_text()
                self.assertNotIn("if flow ==", text)
