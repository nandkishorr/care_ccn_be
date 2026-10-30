import ast
import pathlib

from django.test import SimpleTestCase

import care_ccn_be.protocol as protocol

FORBIDDEN = ("care", "django", "rest_framework", "celery")


class ProtocolPurityTests(SimpleTestCase):
    def test_protocol_is_pure(self):
        root = pathlib.Path(protocol.__file__).parent
        offenders = []
        for path in root.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                modules = []
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                    modules = [node.module]
                offenders += [f"{path.name}: {module}" for module in modules if module.split(".")[0] in FORBIDDEN]
        self.assertEqual(offenders, [])
