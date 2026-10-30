"""Care ``ResourceRequest`` rows of a referral. Idempotent on (coordination id, role): repeated confirms and
callbacks, on either side of a loopback exchange, never create a second origin or assigned request."""

from dataclasses import dataclass
from typing import Any

from care.emr.models.resource_request import ResourceRequest
from care.emr.resources.resource_request.spec import StatusChoices
from django.db import connection

from care_ccn_be import store
from care_ccn_be.context_vars import suppress_beckn_signals
from care_ccn_be.flows.referral.models import ReferralLink, ReferralRole
from care_ccn_be.services import mappers
from care_ccn_be.services.users import system_user

ORIGIN = ReferralRole.ORIGIN.value
ASSIGNED = ReferralRole.ASSIGNED.value


@dataclass
class RequestData:
    origin_facility: Any
    title: str
    reason: str = ""
    category: str = "other"
    emergency: bool = False
    priority: int | None = None
    contact_name: str = ""
    contact_number: str = ""
    assigned_facility: Any = None
    patient: Any = None

    def fields(self) -> dict[str, Any]:
        return {
            "origin_facility": self.origin_facility,
            "assigned_facility": self.assigned_facility,
            "related_patient": self.patient,
            "title": self.title[:255],
            "reason": self.reason,
            "category": self.category,
            "emergency": self.emergency,
            "priority": self.priority,
            "referring_facility_contact_name": self.contact_name,
            "referring_facility_contact_number": self.contact_number,
        }

    @classmethod
    def from_message(cls, message: dict, origin, assigned=None, patient=None) -> "RequestData":
        values = mappers.referral_fields(message)
        return cls(
            origin_facility=origin,
            assigned_facility=assigned,
            patient=patient,
            title=values["title"],
            reason=values["reason"],
            category=values["category"],
            emergency=values["emergency"],
            priority=values["priority"],
            contact_name=values["referring_facility_contact_name"],
            contact_number=values["referring_facility_contact_number"],
        )


def coordination_of(txn) -> str:
    return txn.coordination_id or txn.transaction_id


def _bind(txn) -> str:
    """The coordination id of ``txn``, persisted on it so every lookup by coordination id finds its links."""
    if not txn.coordination_id:
        store.update_transaction(txn, coordination_id=txn.transaction_id)
    return txn.coordination_id


def _links(coordination_id: str, role: str | None = None):
    queryset = ReferralLink.objects.filter(
        transaction__flow="referral", transaction__coordination_id=coordination_id, active=True
    ).select_related("resource_request")
    return queryset.filter(role=role) if role else queryset


def current(coordination_id: str, role: str | None = None) -> ResourceRequest | None:
    """The live request of a referral; without a role the assigned request wins over the origin one."""
    if not coordination_id:
        return None
    link = _links(coordination_id, role).order_by("role", "pk").first()  # "assigned" sorts before "origin"
    return link.resource_request if link else None


def _serialize(coordination_id: str, role: str) -> None:
    """Serialize concurrent creators of the same (coordination id, role) for the current transaction."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", [f"referral:{coordination_id}:{role}"])


def approve(request: ResourceRequest) -> ResourceRequest:
    if request.status == StatusChoices.pending.value:
        request.status = StatusChoices.approved.value
        with suppress_beckn_signals():
            request.save(update_fields=["status", "modified_date"])
    return request


def ensure_request(
    txn, role: str, data: RequestData, approved: bool = False, coordination_id: str | None = None
) -> ResourceRequest:
    """Return the request of (coordination id, role), creating it (and linking it to ``txn``) if missing."""
    coordination_id = coordination_id or _bind(txn)
    _serialize(coordination_id, role)
    request = current(coordination_id, role)
    if request is not None:
        return approve(request) if approved else request
    user = system_user()
    with suppress_beckn_signals():
        request = ResourceRequest(
            **data.fields(),
            status=StatusChoices.approved.value if approved else StatusChoices.pending.value,
            created_by=user,
            updated_by=user,
            extensions={"beckn": {"role": role, "coordinationId": coordination_id}},
        )
        request.save()
    ReferralLink.objects.create(transaction=txn, role=role, resource_request=request, beckn_ref=coordination_id)
    return request


def adopt(txn, role: str, request: ResourceRequest) -> ReferralLink:
    """Link an existing request (created in Care by a user) to its outbound transaction."""
    coordination_id = _bind(txn)
    _serialize(coordination_id, role)
    link, _ = ReferralLink.objects.get_or_create(
        transaction=txn, role=role, active=True, defaults={"resource_request": request, "beckn_ref": coordination_id}
    )
    return link


def complete(coordination_id: str, role: str) -> ResourceRequest | None:
    request = current(coordination_id, role)
    if request is None or request.status == StatusChoices.completed.value:
        return None
    request.status = StatusChoices.completed.value
    with suppress_beckn_signals():
        request.save(update_fields=["status", "modified_date"])
    return request


def origin_request_id(txn) -> str | None:
    request = current(coordination_of(txn), ORIGIN)
    return str(request.external_id) if request else None
