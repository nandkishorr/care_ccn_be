"""The receiving availability a referral contract targets and the request form it carries.

The receiving instance is authoritative: it resolves the availability (only published, active ones accept network
referrals) and validates the answers against its own questionnaire, with Care's referral form rules. Both instances
keep the answered form as a ``ReferralQuestionnaireResponse`` tagged with the form's ref, so it is never synced back.
"""

import uuid
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from care_ccn_be.context_vars import suppress_beckn_signals
from care_ccn_be.exceptions import BecknError
from care_ccn_be.resources.referral.schema import ReferralFormResponse
from care_ccn_be.services import artifacts, mappers
from care_ccn_be.services import availability as availabilities
from care_ccn_be.services.users import system_user

UNAVAILABLE = "70005"
FORM_REQUIRED = "70006"
FORM_INVALID = "70007"
NOT_ACCEPTING = "Facility does not accept network referrals"
OFFER_PREFIX = "offer-"
COMPLETED = "completed"


def _uuid(value: Any) -> str | None:
    try:
        return str(uuid.UUID(str(value))) if value else None
    except (TypeError, ValueError):
        return None


def availability_ref(message: dict[str, Any], from_offer: bool = False) -> str | None:
    """``referral.availability.id``; on select also the catalog offer (``offer-<availability id>``) or resource."""
    block = (mappers.get_contract_attributes(message).get("referral") or {}).get("availability")
    if isinstance(block, dict) and block.get("id"):
        return str(block["id"])
    if not from_offer:
        return None
    for commitment in mappers.get_contract(message).get("commitments") or []:
        for resource in commitment.get("resources") or []:
            found = _uuid(resource.get("id"))
            if found:
                return found
        offer_id = str((commitment.get("offer") or {}).get("id") or "")
        if offer_id.startswith(OFFER_PREFIX) and _uuid(offer_id.removeprefix(OFFER_PREFIX)):
            return offer_id.removeprefix(OFFER_PREFIX)
    return None


def resolve(
    message: dict[str, Any],
    claimed_facility_id: str | None,
    facility,
    referral_type: str,
    from_offer: bool = False,
):
    """The availability receiving the referral, else ``BecknError 70005``; ``None`` if nothing names a target."""
    ref = availability_ref(message, from_offer)
    if ref:
        availability = availabilities.published(ref)
        if availability is None:
            raise BecknError(UNAVAILABLE, f"{NOT_ACCEPTING}: availability {ref} is not open to network referrals")
        owner = availabilities.facility_of(availability)
        if claimed_facility_id and str(owner.external_id) != str(claimed_facility_id):
            raise BecknError(UNAVAILABLE, f"{NOT_ACCEPTING}: availability {ref} belongs to another facility")
        return availability
    if facility is None:
        return None
    availability = availabilities.for_facility(facility, referral_type)
    if availability is None:
        raise BecknError(UNAVAILABLE, f"{NOT_ACCEPTING}: no active {referral_type} referral availability")
    return availability


@dataclass
class RequestForm:
    ref: str
    response: Any  # an unsaved, validated ReferralQuestionnaireResponse


def _describe(detail: Any) -> tuple[str, list[str]]:
    """Care's questionnaire ``ValidationError`` detail as one message and the question ids it is about."""
    if isinstance(detail, dict):
        items = detail.get("errors") if "errors" in detail else [detail]
    elif isinstance(detail, list):
        items = detail
    else:
        items = [detail]
    messages, paths = [], []
    for item in items or []:
        if isinstance(item, dict):
            text = item.get("msg") or item.get("message") or item.get("type") or item
            messages.append(str(text[0] if isinstance(text, list) and text else text))
            if item.get("question_id"):
                paths.append(str(item["question_id"]))
        else:
            messages.append(str(item))
    return "; ".join(messages) or "Invalid request questionnaire answers", paths


def request_form(message: dict[str, Any], availability) -> RequestForm | None:
    """The validated request form of ``message``: required when ``availability`` asks for one (``70006``),
    answers checked against the availability's current questionnaire (``70007``)."""
    from care.emr.api.viewsets.referral import prepare_referral_questionnaire_response
    from care.emr.models.questionnaire import Questionnaire
    from care.emr.models.referral import Referral, ReferralQuestionnaireResponse
    from django.http import Http404
    from rest_framework.exceptions import ValidationError

    required = availabilities.questionnaire_mappings(availability, availabilities.REQUEST).exists()
    form = None
    for item in (mappers.get_contract_attributes(message).get("referral") or {}).get("responses") or []:
        try:
            parsed = ReferralFormResponse.model_validate(item)
        except PydanticValidationError:
            continue
        if parsed.questionnaire_type == availabilities.REQUEST:
            form = parsed
            break
    if form is None:
        if required:
            raise BecknError(FORM_REQUIRED, f"The request questionnaire of {availability.name!r} must be answered")
        return None
    questionnaire_id = _uuid((form.questionnaire or {}).get("id"))
    questionnaire = (
        Questionnaire.objects.filter(external_id=questionnaire_id, deleted=False).first() if questionnaire_id else None
    )
    if questionnaire is None:
        raise BecknError(FORM_INVALID, "Unknown request questionnaire; select the availability again")
    response = ReferralQuestionnaireResponse(
        referral=Referral(availability=availability),
        questionnaire_id=questionnaire.pk,
        questionnaire_response=form.questionnaire_response or {},
    )
    try:
        prepare_referral_questionnaire_response(response, expected_questionnaire_type=availabilities.REQUEST)
    except ValidationError as exc:
        text, paths = _describe(exc.detail)
        raise BecknError(FORM_INVALID, text, paths=paths) from exc
    except PydanticValidationError as exc:
        reasons = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:5])
        raise BecknError(FORM_INVALID, reasons) from exc
    except Http404 as exc:
        raise BecknError(FORM_INVALID, "Unknown request questionnaire; select the availability again") from exc
    return RequestForm(form.ref, response)


def _exists(referral, ref: str) -> bool:
    from care.emr.models.referral import ReferralQuestionnaireResponse

    return ReferralQuestionnaireResponse.objects.filter(
        referral=referral, deleted=False, **{f"meta__{artifacts.CCN_REF}": ref}
    ).exists()


def save(form: RequestForm, referral) -> None:
    """Attach the validated request form to the receiving referral (once)."""
    if _exists(referral, form.ref):
        return
    user = system_user()
    response = form.response
    response.referral = referral
    response.status = COMPLETED
    response.meta = {artifacts.CCN_REF: form.ref}
    response.created_by = user
    response.updated_by = user
    with suppress_beckn_signals():
        response.save()


def save_sent(referral, ref: str, snapshot: dict[str, Any], answers: dict[str, Any]) -> None:
    """The referring side's copy of the request form it sent, on a local shadow of the remote questionnaire."""
    from care.emr.models.referral import ReferralQuestionnaireResponse

    from care_ccn_be.services.referral_sync import shadow_questionnaire

    if _exists(referral, ref):
        return
    user = system_user()
    with suppress_beckn_signals():
        ReferralQuestionnaireResponse.objects.create(
            referral=referral,
            questionnaire=shadow_questionnaire(snapshot, user),
            questionnaire_response=answers or {},
            questionnaire_revision=snapshot.get("internal_revision") or 1,
            questionnaire_snapshot=snapshot,
            questionnaire_type=availabilities.REQUEST,
            status=COMPLETED,
            meta={artifacts.CCN_REF: ref},
            created_by=user,
            updated_by=user,
        )
