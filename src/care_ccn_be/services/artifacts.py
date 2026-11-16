"""Referral files shared over the network: sent as signed read URLs in ``Descriptor.docs[]``, received by downloading
them into Care's own bucket. Received rows carry ``ccn_ref`` so they are never sent back."""

import logging
import re
from functools import partial
from typing import Any
from urllib.parse import urlparse

import requests
from django.conf import settings
from django.db import transaction

from care_ccn_be.context_vars import suppress_beckn_signals
from care_ccn_be.resources.beckn import Document
from care_ccn_be.resources.referral.schema import ReferralAttachment
from care_ccn_be.services import mappers
from care_ccn_be.services.users import system_user
from care_ccn_be.settings import plugin_settings

logger = logging.getLogger(__name__)

CCN_REF = "ccn_ref"
FILE_RESOURCE = "file_upload"
FILE_TYPE = "referral"
FILE_CATEGORY = "unspecified"
CATEGORIES = ("source", "metadata", "response")
DEFAULT_CATEGORY = "source"
EXTENSION = re.compile(r"^(\.[A-Za-z0-9]{1,10}){1,2}$")
CHUNK_SIZE = 64 * 1024
OK = 200
SERVER_ERROR = 500


class ArtifactRejected(Exception):
    """A document that must not be downloaded; retrying cannot help."""


def file_ref(file) -> str:
    return f"file-{file.external_id}"


def export(resource, file) -> tuple[Document, ReferralAttachment]:
    ref = file_ref(file)
    url = file.files_manager.read_signed_url(file, duration=plugin_settings.BECKN_ARTIFACT_URL_TTL)
    document = Document(id=ref, label=ref, name=file.name, url=url, mime_type=(file.meta or {}).get("mime_type"))
    attachment = ReferralAttachment(
        ref=ref, category=resource.attachment_type, name=file.name, extension=file.get_extension() or None
    )
    return document, attachment


def incoming(message: dict[str, Any]) -> list[tuple[dict, dict]]:
    """(document, attachment) pairs of a received message. Documents a non-Care sender lists only as the
    referral note's prior results are shared as ``source`` records."""
    docs = {}
    for doc in (mappers.get_contract(message).get("descriptor") or {}).get("docs") or []:
        if isinstance(doc, dict) and (doc.get("label") or doc.get("id")) and doc.get("url"):
            docs[doc.get("label") or doc.get("id")] = doc
    attributes = mappers.get_contract_attributes(message)
    attachments = {
        item["ref"]: item
        for item in (attributes.get("referral") or {}).get("attachments") or []
        if isinstance(item, dict) and item.get("ref") in docs
    }
    for ref in (attributes.get("referralNote") or {}).get("priorResultRefs") or []:
        if ref in docs and ref not in attachments:
            attachments[ref] = {"ref": ref, "category": DEFAULT_CATEGORY}
    return [(docs[ref], attachment) for ref, attachment in attachments.items()]


def imported(referral, ref: str) -> bool:
    from care.emr.models.referral import ReferralResource

    return ReferralResource.objects.filter(referral=referral, deleted=False, metadata__ccn_ref=ref).exists()


def schedule_imports(referral, message: dict[str, Any]) -> None:
    from care_ccn_be.tasks.referral import import_referral_file

    for document, attachment in incoming(message):
        if not imported(referral, attachment["ref"]):
            transaction.on_commit(partial(import_referral_file.delay, referral.pk, document, attachment))


def download(url: str) -> bytes:
    """Fetch a shared document. Only allow-listed hosts (over https unless DEBUG) and bounded sizes are accepted,
    so a peer cannot point Care at internal services or exhaust its memory."""
    parsed = urlparse(url or "")
    schemes = ("https", "http") if settings.DEBUG else ("https",)
    if parsed.scheme not in schemes or not parsed.hostname:
        raise ArtifactRejected(f"unsupported document url scheme {parsed.scheme!r}")
    allowed = {host.lower() for host in plugin_settings.BECKN_ARTIFACT_ALLOWED_HOSTS}
    if parsed.hostname.lower() not in allowed:
        raise ArtifactRejected(f"{parsed.hostname} is not an allowed artifact host")
    limit = plugin_settings.BECKN_ARTIFACT_MAX_BYTES
    timeout = plugin_settings.BECKN_HTTP_TIMEOUT
    with requests.get(url, stream=True, timeout=timeout, allow_redirects=False) as response:
        if response.status_code >= SERVER_ERROR:
            response.raise_for_status()
        if response.status_code != OK:
            raise ArtifactRejected(f"document url answered {response.status_code}")
        if int(response.headers.get("Content-Length") or 0) > limit:
            raise ArtifactRejected("document is larger than BECKN_ARTIFACT_MAX_BYTES")
        content = bytearray()
        for chunk in response.iter_content(CHUNK_SIZE):
            content.extend(chunk)
            if len(content) > limit:
                raise ArtifactRejected("document is larger than BECKN_ARTIFACT_MAX_BYTES")
    return bytes(content)


def validate(document: dict, attachment: dict) -> tuple[str, str, str]:
    """(mime type, name, extension) of a received document; Care's upload rules apply."""
    mime_type = document.get("mimeType") or ""
    if mime_type not in settings.ALLOWED_MIME_TYPES:
        raise ArtifactRejected(f"mime type {mime_type!r} is not allowed")
    name = (attachment.get("name") or document.get("name") or attachment["ref"]).strip()[:200] or attachment["ref"]
    extension = attachment.get("extension") or ""
    return mime_type, name, extension if EXTENSION.match(extension) else ""


def save(referral, document: dict, attachment: dict, content: bytes):
    """Store a downloaded document as a completed referral file and share it on ``referral``."""
    from care.emr.models.file_upload import FileUpload
    from care.emr.models.referral import ReferralResource

    mime_type, name, extension = validate(document, attachment)
    category = attachment.get("category") if attachment.get("category") in CATEGORIES else DEFAULT_CATEGORY
    ref = attachment["ref"]
    user = system_user()
    with suppress_beckn_signals():
        file = FileUpload(
            name=name,
            internal_name=f"{name}{extension}",
            file_type=FILE_TYPE,
            file_category=FILE_CATEGORY,
            associating_id=str(referral.external_id),
            upload_completed=True,
            meta={"mime_type": mime_type, CCN_REF: ref},
            created_by=user,
            updated_by=user,
        )
        file.save()
        file.files_manager.put_object(file, content, ContentType=mime_type)
        ReferralResource.objects.create(
            referral=referral,
            resource_type=FILE_RESOURCE,
            resource_id=file.external_id,
            attachment_type=category,
            metadata={CCN_REF: ref},
            created_by=user,
            updated_by=user,
        )
    return file
