"""``ReferralAvailability`` rows the plugin owns: where a Care referral is going.

* A *network* availability (``entity_type=external``) stands for a remote coordinator or facility reached through
  Beckn; its ``external_entity["beckn"]`` carries the routing. A pending referral on one is sent to that target.
* A *facility* availability receives referrals that arrive from the network. An availability the facility already
  configured is reused (so its care team becomes the assignees); otherwise a plain one is created.
"""

from copy import deepcopy

from care.emr.models.referral import ReferralAvailability
from care.emr.resources.referral.common import (
    ReferralAvailabilityStatus,
    ReferralEntityType,
    ReferralType,
)

from care_ccn_be.services.users import system_user

EXTERNAL_KEY = "beckn"
DEFAULT_REFERRAL_TYPE = ReferralType.consultation.value
REFERRAL_TYPES = tuple(item.value for item in ReferralType)


def target_of(availability) -> dict:
    """The Beckn routing of a network availability, ``{}`` for any other availability."""
    if availability is None or availability.entity_type != ReferralEntityType.external.value:
        return {}
    return (availability.external_entity or {}).get(EXTERNAL_KEY) or {}


def is_network(availability) -> bool:
    return bool(target_of(availability))


def network(
    bpp_id: str,
    bpp_uri: str,
    referral_type: str = DEFAULT_REFERRAL_TYPE,
    assigned_facility_id: str | None = None,
    name: str | None = None,
) -> ReferralAvailability:
    """The public, active network availability of (target, referral type), created on first use."""
    target = {"bppId": bpp_id, "bppUri": bpp_uri}
    if assigned_facility_id:
        target["assignedFacilityId"] = str(assigned_facility_id)
    external_entity = {EXTERNAL_KEY: target}
    existing = (
        ReferralAvailability.objects.filter(
            entity_type=ReferralEntityType.external.value,
            referral_type=referral_type,
            external_entity=external_entity,
            deleted=False,
        )
        .order_by("pk")
        .first()
    )
    if existing is not None:
        return existing
    user = system_user()
    return ReferralAvailability.objects.create(
        entity_type=ReferralEntityType.external.value,
        external_entity=external_entity,
        status=ReferralAvailabilityStatus.active.value,
        is_public=True,
        referral_type=referral_type,
        name=(name or f"Beckn {referral_type} referral - {bpp_id or bpp_uri}")[:255],
        description="Referral sent through the Beckn network.",
        created_by=user,
        updated_by=user,
    )


def for_facility(facility, referral_type: str = DEFAULT_REFERRAL_TYPE) -> ReferralAvailability:
    """The availability that receives network referrals for ``facility``, created on first use."""
    existing = (
        ReferralAvailability.objects.filter(
            entity_type=ReferralEntityType.facility.value,
            facility=facility,
            referral_type=referral_type,
            status=ReferralAvailabilityStatus.active.value,
            deleted=False,
        )
        .order_by("pk")
        .first()
    )
    if existing is not None:
        return existing
    user = system_user()
    return ReferralAvailability.objects.create(
        entity_type=ReferralEntityType.facility.value,
        facility=facility,
        status=ReferralAvailabilityStatus.active.value,
        referral_type=referral_type,
        name=f"Network {referral_type} referrals - {facility.name}"[:255],
        description="Referrals received through the Beckn network.",
        created_by=user,
        updated_by=user,
    )


def assignees(availability) -> list:
    """The care team of the availability, copied the way Care's referral API copies it on creation."""
    return deepcopy(availability.care_team or [])
