"""Keeping the two Care referrals of a coordination in step: what one side still owes the other (shared files,
answered forms, the receiving side's status) and how what the other side sent is mirrored locally."""

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from django.db import transaction
from pydantic import ValidationError

from care_ccn_be.context_vars import suppress_beckn_signals
from care_ccn_be.resources.builders.referral import sync_message
from care_ccn_be.resources.referral.schema import ReferralFormResponse
from care_ccn_be.services import artifacts, mappers
from care_ccn_be.services import referral as lifecycle
from care_ccn_be.services.users import system_user

logger = logging.getLogger(__name__)

CCN_REF = artifacts.CCN_REF
SYNCED = "synced"
RESPONSE_COMPLETED = "completed"
QUESTIONNAIRE_TYPES = ("request", "response")
SHADOW_STATUS = "retired"  # never offered for filling; it only anchors mirrored responses
REFERRAL_SUBJECT = "referral"


@dataclass
class Outbox:
    files: list = field(default_factory=list)  # (ReferralResource, FileUpload)
    responses: list = field(default_factory=list)
    status: str | None = None

    def __bool__(self) -> bool:
        return bool(self.files or self.responses or self.status)


def schedule(referral_id: int) -> None:
    from care_ccn_be.tasks.referral import sync_referral

    transaction.on_commit(lambda: sync_referral.delay(referral_id))


def pending(link) -> Outbox:
    """What was added on the linked referral and not yet sent; rows mirrored from the other side are skipped."""
    from care.emr.models.file_upload import FileUpload
    from care.emr.models.referral import ReferralQuestionnaireResponse, ReferralResource

    referral = link.referral
    synced = (link.details or {}).get(SYNCED) or {}
    sent_files = set(synced.get("files") or [])
    sent_responses = set(synced.get("responses") or [])
    resources = {
        str(resource.resource_id): resource
        for resource in ReferralResource.objects.filter(
            referral=referral, resource_type=artifacts.FILE_RESOURCE, deleted=False
        )
        .exclude(metadata__has_key=CCN_REF)
        .order_by("pk")
        if str(resource.resource_id) not in sent_files
    }
    files = FileUpload.objects.filter(
        external_id__in=list(resources), deleted=False, upload_completed=True, is_archived=False
    ).order_by("pk")
    responses = (
        ReferralQuestionnaireResponse.objects.filter(referral=referral, status=RESPONSE_COMPLETED, deleted=False)
        .exclude(meta__has_key=CCN_REF)
        .select_related("created_by")
        .order_by("pk")
    )
    outbox = Outbox(
        files=[(resources[str(file.external_id)], file) for file in files],
        responses=[response for response in responses if str(response.external_id) not in sent_responses],
    )
    # both referrals are in progress once confirmed, so only later changes are reported
    reported = synced.get("status", lifecycle.IN_PROGRESS)
    if link.role == lifecycle.ASSIGNED and referral.status not in (reported, lifecycle.PENDING):
        outbox.status = referral.status
    return outbox


def mark_sent(link, outbox: Outbox) -> None:
    details = dict(link.details or {})
    synced = dict(details.get(SYNCED) or {})
    synced["files"] = [*(synced.get("files") or []), *(str(file.external_id) for _, file in outbox.files)]
    synced["responses"] = [*(synced.get("responses") or []), *(str(r.external_id) for r in outbox.responses)]
    if outbox.status:
        synced["status"] = outbox.status
    details[SYNCED] = synced
    link.details = details
    link.save(update_fields=["details"])


def author(user) -> str | None:
    if user is None:
        return None
    return " ".join(filter(None, [user.first_name, user.last_name])) or user.username


def form_response(response) -> ReferralFormResponse:
    return ReferralFormResponse(
        ref=f"response-{response.external_id}",
        questionnaire_type=response.questionnaire_type,
        status=response.status,
        revision=response.questionnaire_revision,
        questionnaire=response.questionnaire_snapshot,
        questionnaire_response=response.questionnaire_response,
        created_at=response.created_date,
        author=author(response.created_by),
    )


def message(
    coordination_id: str, outbox: Outbox, contract_id: str | None = None, external_referral_id: str | None = None
) -> dict[str, Any]:
    exported = [artifacts.export(resource, file) for resource, file in outbox.files]
    return sync_message(
        coordination_id,
        [document for document, _ in exported],
        [attachment for _, attachment in exported],
        [form_response(response) for response in outbox.responses],
        status=outbox.status,
        contract_id=contract_id,
        external_referral_id=external_referral_id,
    )


# ----------------------------------------------------------------------------- receiving
def receive(referral, message: dict[str, Any], mirror_status: bool) -> None:
    """Mirror what the other side sent onto ``referral``; documents are downloaded after commit."""
    block = mappers.get_contract_attributes(message).get("referral") or {}
    import_responses(referral, block.get("responses") or [])
    if mirror_status:
        lifecycle.advance(referral, block.get("status"))
    artifacts.schedule_imports(referral, message)


def import_responses(referral, items: list[Any]) -> None:
    from care.emr.models.referral import ReferralQuestionnaireResponse

    user = None
    for item in items:
        try:
            response = ReferralFormResponse.model_validate(item)
        except ValidationError:
            logger.warning("ignoring an invalid referral form response from the network")
            continue
        if (
            response.status != RESPONSE_COMPLETED
            or response.questionnaire_type not in QUESTIONNAIRE_TYPES
            or not isinstance(response.questionnaire, dict)
        ):
            continue
        if ReferralQuestionnaireResponse.objects.filter(referral=referral, meta__ccn_ref=response.ref).exists():
            continue
        user = user or system_user()
        with suppress_beckn_signals():
            ReferralQuestionnaireResponse.objects.create(
                referral=referral,
                questionnaire=shadow_questionnaire(response.questionnaire, user),
                questionnaire_response=response.questionnaire_response or {},
                questionnaire_revision=response.revision or 1,
                questionnaire_snapshot=response.questionnaire,
                questionnaire_type=response.questionnaire_type,
                status=RESPONSE_COMPLETED,
                meta={CCN_REF: response.ref, "ccn_author": response.author},
                created_by=user,
                updated_by=user,
            )


def shadow_questionnaire(snapshot: dict[str, Any], user):
    """A local, retired copy of the other side's form: Care's responses need a questionnaire row, while the
    UI renders the snapshot stored on the response."""
    from care.emr.models.questionnaire import Questionnaire

    questions = snapshot.get("questions") if isinstance(snapshot.get("questions"), list) else []
    digest = hashlib.sha256(
        json.dumps({"title": snapshot.get("title"), "questions": questions}, sort_keys=True, default=str).encode()
    ).hexdigest()
    slug = f"ccn-{digest[:40]}"
    existing = Questionnaire.objects.filter(slug=slug, deleted=False, latest_revision__isnull=True).first()
    if existing is not None:
        return existing
    with suppress_beckn_signals():
        return Questionnaire.objects.create(
            slug=slug,
            title=str(snapshot.get("title") or "Network form")[:255],
            description=str(snapshot.get("description") or ""),
            version=str(snapshot.get("version") or "1.0")[:255],
            subject_type=REFERRAL_SUBJECT,
            status=SHADOW_STATUS,
            questions=questions,
            questions_hash=digest,
            created_by=user,
            updated_by=user,
        )
