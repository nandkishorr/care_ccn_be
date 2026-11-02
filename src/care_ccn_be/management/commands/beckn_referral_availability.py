"""Create the network referral availabilities Care users pick when they refer a patient through Beckn.

python manage.py beckn_referral_availability                      # the configured coordination centre
python manage.py beckn_referral_availability --bpp-id x --bpp-uri https://x --assigned-facility <facility id>

One availability exists per (target, referral type); running it again reuses it. A pending Care referral raised on
one of them is sent to that target.
"""

from django.core.management.base import BaseCommand, CommandError

from care_ccn_be.identity import cc_endpoint
from care_ccn_be.services import availability as availabilities


class Command(BaseCommand):
    help = "Create the network ReferralAvailability rows that route Care referrals through Beckn."

    def add_arguments(self, parser):
        parser.add_argument("--bpp-id", help="Target BPP id (default: BECKN_CC_BPP_ID).")
        parser.add_argument("--bpp-uri", help="Target BPP URI (default: BECKN_CC_BPP_URI).")
        parser.add_argument("--assigned-facility", help="External id of the facility the referral is meant for.")
        parser.add_argument("--name", help="Display name of the availability.")
        parser.add_argument(
            "--type",
            action="append",
            choices=availabilities.REFERRAL_TYPES,
            dest="types",
            help="Referral type; repeat for several (default: all).",
        )

    def handle(self, *args, **options):
        default = cc_endpoint()
        bpp_id, bpp_uri = options["bpp_id"] or default.id, options["bpp_uri"] or default.uri
        if not (bpp_id and bpp_uri):
            raise CommandError("Give --bpp-id and --bpp-uri, or set BECKN_CC_BPP_ID and BECKN_CC_BPP_URI.")
        for referral_type in options["types"] or availabilities.REFERRAL_TYPES:
            availability = availabilities.network(
                bpp_id, bpp_uri, referral_type, options["assigned_facility"], options["name"]
            )
            self.stdout.write(f"{referral_type}: {availability.external_id} ({availability.name})")
