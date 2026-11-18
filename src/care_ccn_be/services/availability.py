"""``ReferralAvailability`` rows of the referral flow: where a Care referral is going, and what may receive one.

* A *network* availability (``entity_type=external``) stands for a remote coordinator or facility reached through
  Beckn; its ``external_entity["beckn"]`` carries the routing (and the remote availability id, when known). A
  pending referral on one is sent to that target.
* A *receiving* availability (``facility`` or ``internal_organization``) is configured in Care. Only facilities that
  have one receive network referrals: its care team becomes the assignees and its forms are asked for.
"""

import uuid
from copy import deepcopy
from typing import Any

from care.emr.models.referral import ReferralAvailability, ReferralAvailabilityQuestionnaire
from care.emr.resources.referral.common import (
    ReferralAvailabilityStatus,
    ReferralEntityType,
    ReferralQuestionnaireType,
    ReferralType,
)
from django.db.models import Q

from care_ccn_be.context_vars import suppress_beckn_signals
from care_ccn_be.resources.referral.schema import AvailabilityQuestionnaire, ReferralAvailabilityBlock
from care_ccn_be.services.users import system_user

EXTERNAL_KEY = "beckn"
DEFAULT_REFERRAL_TYPE = ReferralType.consultation.value
REFERRAL_TYPES = tuple(item.value for item in ReferralType)
REQUEST = ReferralQuestionnaireType.request.value
RESPONSE = ReferralQuestionnaireType.response.value
RECEIVING_TYPES = (ReferralEntityType.facility.value, ReferralEntityType.internal_organization.value)
REFERRAL_SUBJECT = "referral"
SNAPSHOT_PRIVATE_KEYS = ("created_by", "updated_by")


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
    availability_id: str | None = None,
) -> ReferralAvailability:
    """The public, active network availability of (target, referral type, remote availability), created on first
    use; a known remote availability keeps its name in step."""
    target = {"bppId": bpp_id, "bppUri": bpp_uri}
    if assigned_facility_id:
        target["assignedFacilityId"] = str(assigned_facility_id)
    if availability_id:
        target["availabilityId"] = str(availability_id)
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
        if name and availability_id and existing.name != name[:255]:
            existing.name = name[:255]
            with suppress_beckn_signals():
                existing.save(update_fields=["name", "modified_date"])
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


def link_questionnaires(availability, block: ReferralAvailabilityBlock) -> None:
    """Map the remote forms (as local shadow questionnaires) onto the network availability that stands for them."""
    from care_ccn_be.services.referral_sync import shadow_questionnaire

    user = None
    wanted = set()
    for item in block.questionnaires or []:
        if item.questionnaire_type not in (REQUEST, RESPONSE):
            continue
        user = user or system_user()
        questionnaire = shadow_questionnaire(item.questionnaire, user)
        wanted.add(questionnaire.pk)
        ReferralAvailabilityQuestionnaire.objects.update_or_create(
            availability=availability,
            questionnaire=questionnaire,
            defaults={"questionnaire_type": item.questionnaire_type},
        )
    availability.questionnaire_mappings.exclude(questionnaire_id__in=wanted).delete()


# ----------------------------------------------------------------------------- receiving availabilities
def receiving(public_only: bool = False):
    """Active availabilities a facility (directly or through one of its organizations) receives referrals on."""
    queryset = ReferralAvailability.objects.filter(
        entity_type__in=RECEIVING_TYPES, status=ReferralAvailabilityStatus.active.value, deleted=False
    ).select_related("facility", "internal_organization__facility")
    return queryset.filter(is_public=True) if public_only else queryset


def facility_of(availability):
    if availability.entity_type == ReferralEntityType.internal_organization.value:
        return availability.internal_organization.facility
    return availability.facility


def published(external_id) -> ReferralAvailability | None:
    """The public receiving availability ``external_id`` of this instance, ``None`` for any other id."""
    try:
        uuid.UUID(str(external_id))
    except (TypeError, ValueError):
        return None
    return receiving(public_only=True).filter(external_id=external_id).first()


def for_facility(facility, referral_type: str = DEFAULT_REFERRAL_TYPE) -> ReferralAvailability | None:
    """The availability ``facility`` receives ``referral_type`` network referrals on; never created implicitly."""
    return (
        receiving()
        .filter(Q(facility=facility) | Q(internal_organization__facility=facility), referral_type=referral_type)
        .order_by("-is_public", "pk")
        .first()
    )


def snapshot(questionnaire) -> dict[str, Any]:
    """Care's read shape of a questionnaire, without the users who wrote it."""
    from care.emr.resources.questionnaire.spec import QuestionnaireReadSpec

    data = QuestionnaireReadSpec.serialize(questionnaire).to_json()
    for key in SNAPSHOT_PRIVATE_KEYS:
        data.pop(key, None)
    return data


def questionnaire_mappings(availability, questionnaire_type: str | None = None):
    """The current, active referral forms of ``availability``, as Care's referral API lists them."""
    mappings = availability.questionnaire_mappings.filter(
        questionnaire__deleted=False,
        questionnaire__status="active",
        questionnaire__latest_revision__isnull=True,
        questionnaire__subject_type=REFERRAL_SUBJECT,
    ).select_related("questionnaire")
    if questionnaire_type:
        mappings = mappings.filter(questionnaire_type=questionnaire_type)
    return mappings.order_by("pk")


def block(availability, with_questionnaires: bool = True) -> ReferralAvailabilityBlock:
    facility = facility_of(availability)
    questionnaires = None
    if with_questionnaires:
        questionnaires = [
            AvailabilityQuestionnaire(
                questionnaire_type=mapping.questionnaire_type, questionnaire=snapshot(mapping.questionnaire)
            )
            for mapping in questionnaire_mappings(availability)
        ]
    return ReferralAvailabilityBlock(
        id=str(availability.external_id),
        name=availability.name,
        description=availability.description or None,
        referral_type=availability.referral_type,
        facility_id=str(facility.external_id) if facility else None,
        facility_name=facility.name if facility else None,
        questionnaires=questionnaires,
    )


def assignees(availability) -> list:
    """The care team of the availability, copied the way Care's referral API copies it on creation."""
    return deepcopy(availability.care_team or [])
