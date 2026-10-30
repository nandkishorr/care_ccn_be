"""Publish the catalog of every enabled flow (or one flow) to the network.

python manage.py publish_beckn_catalog --dry-run
python manage.py publish_beckn_catalog --flow appointment
python manage.py publish_beckn_catalog --all
"""

import json

from django.core.management.base import BaseCommand, CommandError

from care_ccn_be.exceptions import BecknError
from care_ccn_be.flows.registry import FlowRegistry
from care_ccn_be.services.publisher import publish_catalog


class Command(BaseCommand):
    help = "Publish the Beckn catalog of the enabled flows (catalog/publish via the BPP caller)."

    def add_arguments(self, parser):
        parser.add_argument("--flow", help="Publish only this flow's catalog (default: every enabled flow with one).")
        parser.add_argument(
            "--dry-run", action="store_true", help="Build and print the payload; record and send nothing."
        )
        parser.add_argument("--all", action="store_true", help="Include non-public schedules (default: public only).")
        parser.add_argument(
            "--coordination", action="store_true", help="Shorthand for --flow referral (the care-coordinator catalog)."
        )

    def handle(self, *args, **options):
        name = "referral" if options["coordination"] else options["flow"]
        flows = self.flows(name)
        published = 0
        for flow in flows:
            try:
                result = publish_catalog(flow, public_only=not options["all"], dry_run=options["dry_run"])
            except BecknError as exc:
                if name:
                    raise CommandError(exc.message) from exc
                continue
            published += 1
            if result.dry_run:
                self.stdout.write(json.dumps(result.payload, indent=2, default=str))
                self.stdout.write(
                    self.style.SUCCESS(
                        f"[{flow.name}] dry run: {len(result.catalogs)} catalog(s) built, not published."
                    )
                )
            else:
                self.stdout.write(
                    self.style.SUCCESS(f"[{flow.name}] queued {len(result.catalogs)} catalog(s) for publishing.")
                )
        if not published:
            raise CommandError("no enabled flow publishes a catalog")

    @staticmethod
    def flows(name: str | None):
        if not name:
            return [flow for flow in FlowRegistry.enabled() if flow.catalog_builder is not None]
        try:
            flow = FlowRegistry.get(name)
        except (KeyError, LookupError) as exc:
            raise CommandError(f"unknown flow {name!r}") from exc
        if not FlowRegistry.is_enabled(flow.name):
            raise CommandError(f"flow {flow.name!r} is not enabled")
        return [flow]
