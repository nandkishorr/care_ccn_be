"""Import legacy ``care.beckn`` data into the plugin tables.

python manage.py beckn_backfill --dry-run --since 2026-10-01
python manage.py beckn_backfill --only referral
python manage.py beckn_backfill --strip-legacy [--force]
"""

from datetime import datetime

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from care_ccn_be.services import backfill


class Command(BaseCommand):
    help = (
        "Backfill plugin transactions, messages and links from legacy core Beckn data (idempotent, never calls ONIX)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Run everything, then roll back and report.")
        parser.add_argument("--since", help="Only import rows created on or after this date (YYYY-MM-DD).")
        parser.add_argument("--only", choices=sorted(backfill.ONLY), help="Import one family of shapes.")
        parser.add_argument(
            "--strip-legacy",
            action="store_true",
            help="Reduce the legacy blobs on Care rows (after the rollback window). Imports nothing.",
        )
        parser.add_argument("--force", action="store_true", help="With --strip-legacy: ignore rows that have no link.")

    def handle(self, *args, **options):
        if options["strip_legacy"]:
            return self.strip(options)
        since = None
        if options["since"]:
            try:
                since = timezone.make_aware(datetime.strptime(options["since"], "%Y-%m-%d"))
            except ValueError as exc:
                raise CommandError("--since must be YYYY-MM-DD") from exc
        reports = backfill.Backfill(dry_run=options["dry_run"], since=since, only=options["only"]).run()
        self.stdout.write(f"{'DRY RUN: ' if options['dry_run'] else ''}{'shape':26}created existing ambiguous skipped")
        for name, report in reports.items():
            self.stdout.write(
                f"{name:26}{report.created:7} {report.existing:8} {report.ambiguous:9} {report.skipped:7}"
            )

    def strip(self, options):
        missing = backfill.unlinked_legacy_rows()
        if missing and not options["force"]:
            raise CommandError(f"{missing} legacy row(s) have no link; run beckn_backfill first or pass --force.")
        counts = backfill.strip_legacy(dry_run=options["dry_run"])
        self.stdout.write(f"{'DRY RUN: ' if options['dry_run'] else ''}stripped {counts}")
