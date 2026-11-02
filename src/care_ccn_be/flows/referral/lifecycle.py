"""Care ``Referral`` rows of a referral. Idempotent on (coordination id, role): repeated confirms and callbacks,
on either side of a loopback exchange, never create a second origin or assigned referral."""

from dataclasses import dataclass, field
from typing import Any

from care.emr.models.referral import Referral
from care.emr.resources.referral.spec import ReferralStatus
from django.db import connection

from care_ccn_be import store
from care_ccn_be.context_vars import suppress_beckn_signals
from care_ccn_be.flows.referral.models import ReferralLink, ReferralRole
from care_ccn_be.services import mappers
from care_ccn_be.services.users import system_user

ORIGIN = ReferralRole.ORIGIN.value
ASSIGNED = ReferralRole.ASSIGNED.value

PENDING = ReferralStatus.pending.value
IN_PROGRESS = ReferralStatus.in_progress.value
COMPLETED = ReferralStatus.completed.value


@dataclass
class ReferralData:
    patient: Any
    availability: Any
    title: str
    note: str = ""
    urgency: str = mappers.URGENCY_ROUTINE
    origin_facility_id: str | None = None
    contact_name: str = ""
    contact_number: str = ""
    assignees: list = field(default_factory=list)

    def fields(self) -> dict[str, Any]:
        return {
            "availability": self.availability,
            "patient": self.patient,
            "title": self.title[:255] or "Care referral",
            "note": self.note,
            "assignees": self.assignees,
        }

    def details(self) -> dict[str, Any]:
        """What Care's Referral has no column for; read back when echoing the referral to the network."""
        details: dict[str, Any] = {"urgency": self.urgency}
        if self.origin_facility_id:
            details["originFacilityId"] = self.origin_facility_id
        if self.contact_name or self.contact_number:
            details["contact"] = {"name": self.contact_name, "number": self.contact_number}
        return details

    @classmethod
    def from_message(
        cls, message: dict, availability, patient, origin_facility_id: str | None = None, assignees=()
    ) -> "ReferralData":
        values = mappers.referral_fields(message)
        return cls(
            patient=patient,
            availability=availability,
            title=values["title"],
            note=values["note"],
            urgency=values["urgency"],
            origin_facility_id=origin_facility_id,
            contact_name=values["referring_facility_contact_name"],
            contact_number=values["referring_facility_contact_number"],
            assignees=list(assignees),
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
    ).select_related("referral", "referral__availability", "referral__patient")
    return queryset.filter(role=role) if role else queryset


def current_link(coordination_id: str, role: str | None = None) -> ReferralLink | None:
    """The live link of a referral; without a role the assigned one wins over the origin one."""
    if not coordination_id:
        return None
    return _links(coordination_id, role).order_by("role", "pk").first()  # "assigned" sorts before "origin"


def current(coordination_id: str, role: str | None = None) -> Referral | None:
    link = current_link(coordination_id, role)
    return link.referral if link else None


def _serialize(coordination_id: str, role: str) -> None:
    """Serialize concurrent creators of the same (coordination id, role) for the current transaction."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", [f"referral:{coordination_id}:{role}"])


def _set_status(referral: Referral, status: str) -> Referral:
    referral.status = status
    with suppress_beckn_signals():
        referral.save(update_fields=["status", "modified_date"])
    return referral


def approve(referral: Referral) -> Referral:
    return _set_status(referral, IN_PROGRESS) if referral.status == PENDING else referral


def ensure_referral(
    txn, role: str, data: ReferralData, approved: bool = False, coordination_id: str | None = None
) -> Referral:
    """Return the referral of (coordination id, role), creating it (and linking it to ``txn``) if missing."""
    coordination_id = coordination_id or _bind(txn)
    _serialize(coordination_id, role)
    referral = current(coordination_id, role)
    if referral is not None:
        return approve(referral) if approved else referral
    user = system_user()
    with suppress_beckn_signals():
        referral = Referral(
            **data.fields(), status=IN_PROGRESS if approved else PENDING, created_by=user, updated_by=user
        )
        referral.save()
    ReferralLink.objects.create(
        transaction=txn, role=role, referral=referral, beckn_ref=coordination_id, details=data.details()
    )
    return referral


def adopt(txn, role: str, referral: Referral, details: dict[str, Any] | None = None) -> ReferralLink:
    """Link an existing referral (created in Care by a user) to its outbound transaction."""
    coordination_id = _bind(txn)
    _serialize(coordination_id, role)
    link, _ = ReferralLink.objects.get_or_create(
        transaction=txn,
        role=role,
        active=True,
        defaults={"referral": referral, "beckn_ref": coordination_id, "details": details or {}},
    )
    return link


def complete(coordination_id: str, role: str) -> Referral | None:
    referral = current(coordination_id, role)
    if referral is None or referral.status == COMPLETED:
        return None
    return _set_status(referral, COMPLETED)


def origin_referral_id(txn) -> str | None:
    referral = current(coordination_of(txn), ORIGIN)
    return str(referral.external_id) if referral else None
