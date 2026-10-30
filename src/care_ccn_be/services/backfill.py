"""Imports legacy ``care.beckn`` data (blobs on Care rows, Redis records) into the plugin tables.

Idempotent (rows are keyed by ``(transaction_id, side)`` and the link constraints), batched, never calls ONIX and never
deletes. Works whether or not the plugin's flows are enabled, so it can run before the cutover.
"""

from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from care.emr.models.patient import Patient
from care.emr.models.resource_request import ResourceRequest
from care.emr.models.scheduling.booking import TokenBooking
from django.core.cache import cache
from django.db import transaction

from care_ccn_be import store
from care_ccn_be.flows.appointment.models import AppointmentLink
from care_ccn_be.flows.referral.models import ReferralLink, ReferralRole
from care_ccn_be.identity import cc_endpoint
from care_ccn_be.models import Delivery, Direction, Processing, Side
from care_ccn_be.settings import plugin_settings

BATCH = 500
REFERRAL, APPOINTMENT = "referral", "appointment"
SHAPES = {
    "referral_confirm": 1,
    "referral_init": 2,
    "referral_on_confirm": 3,
    "referral_sent": 4,
    "appointment_booking": 5,
    "redis_booking_referral": 6,
    "redis_transaction": 7,
}
ONLY = {"referral": (1, 2, 3, 4), "appointment": (5,), "redis": (6, 7)}
OPEN_REFERRAL_STATUSES = ("pending", "approved", "transportation_to_be_arranged", "transfer_in_progress")
SERVICE_TYPE_FLOWS = {
    "appointment": APPOINTMENT,
    "referral": REFERRAL,
    "consultation": REFERRAL,
    "coordination": REFERRAL,
}
TXN_PREFIX = "beckn:txn:"
BOOKING_PREFIX = "beckn:booking-referral:"


@dataclass
class Report:
    created: int = 0
    existing: int = 0
    ambiguous: int = 0
    skipped: int = 0


@dataclass
class Backfill:
    dry_run: bool = False
    since: datetime | None = None
    only: str | None = None
    reports: dict[str, Report] = field(default_factory=lambda: {name: Report() for name in SHAPES})

    def wanted(self, shape: int) -> bool:
        return not self.only or shape in ONLY[self.only]

    def run(self) -> dict[str, Report]:
        if self.wanted(1):
            self.referrals()
        if self.wanted(5):
            self.appointments()
        if self.wanted(6):
            self.booking_links()
        if self.wanted(7):
            self.redis_transactions()
        return self.reports

    # ------------------------------------------------------------------------------------ batching
    def batched(self, items: Iterable[Any], handle: Callable[[Any], None]) -> None:
        """One DB transaction per batch; ``--dry-run`` rolls each batch back."""
        buffer: list[Any] = []
        for item in items:
            buffer.append(item)
            if len(buffer) >= BATCH:
                self.commit(buffer, handle)
                buffer = []
        if buffer:
            self.commit(buffer, handle)

    def commit(self, items: list[Any], handle: Callable[[Any], None]) -> None:
        with transaction.atomic():
            for item in items:
                handle(item)
            if self.dry_run:
                transaction.set_rollback(True)

    def count(self, shape: str, outcome: str) -> None:
        setattr(self.reports[shape], outcome, getattr(self.reports[shape], outcome) + 1)

    def outcome(self, shape: str, created: bool) -> None:
        self.count(shape, "created" if created else "existing")

    # ------------------------------------------------------------------------------------ referrals (1-4)
    def referrals(self) -> None:
        legacy = ResourceRequest.objects.filter(extensions__has_key="beckn")
        if self.since:
            legacy = legacy.filter(created_date__gte=self.since)
        self.batched(legacy.order_by("pk").iterator(chunk_size=BATCH), self.referral_blob)
        if self.wanted(4):
            self.sent_referrals()

    def referral_blob(self, rr: ResourceRequest) -> None:
        blob = rr.extensions.get("beckn") or {}
        role, tid = blob.get("role"), blob.get("transactionId")
        if role and blob.get("returnRouting"):
            self.referral_confirm(rr, blob, role)
        elif role and tid:
            self.referral_init(rr, blob, role)
        elif tid and blob.get("contract"):
            self.referral_on_confirm(rr, blob)
        else:
            self.count("referral_init", "skipped")

    def referral_confirm(self, rr, blob: dict, role: str) -> None:
        routing = blob["returnRouting"]
        contract = blob.get("contract") or {}
        txn, created = store.backfill_transaction(
            blob["transactionId"],
            Side.BPP,
            REFERRAL,
            "referral_confirm",
            status="ON_CONFIRM",
            counterparty_id=routing.get("bapId", ""),
            counterparty_uri=routing.get("bapUri", ""),
            contract_id=str(contract.get("id") or ""),
            coordination_id=blob.get("coordinationId") or blob["transactionId"],
            patient_id=rr.related_patient_id,
            facility_id=rr.assigned_facility_id or rr.origin_facility_id,
        )
        context = {
            "transactionId": txn.transaction_id,
            "messageId": f"legacy-{txn.transaction_id}",
            "action": "confirm",
        }
        payload = {"context": context, "message": {"contract": contract}}
        _, msg_created = store.backfill_message(
            txn,
            context["messageId"],
            "confirm",
            Direction.IN,
            payload,
            sender_id=routing.get("bapId") or routing.get("bapUri") or "",
            processing_status=Processing.PROCESSED,
        )
        linked = self.link_referral(txn, rr, role)
        self.outcome("referral_confirm", created or msg_created or linked)

    def referral_init(self, rr, blob: dict, role: str) -> None:
        tid = blob["transactionId"]
        on_bap_side = cache.get(f"{TXN_PREFIX}{tid}") is not None
        side = Side.BAP if on_bap_side else Side.BPP
        txn, created = store.backfill_transaction(
            tid,
            side,
            REFERRAL,
            "referral_init",
            status="ON_INIT",
            coordination_id=blob.get("coordinationId") or tid,
            patient_id=rr.related_patient_id,
            facility_id=rr.origin_facility_id if role == ReferralRole.ORIGIN else rr.assigned_facility_id,
        )
        linked = self.link_referral(txn, rr, role)
        self.outcome("referral_init", created or linked)
        if created and not on_bap_side:
            self.count("referral_init", "ambiguous")

    def referral_on_confirm(self, rr, blob: dict) -> None:
        tid = blob["transactionId"]
        cc = cc_endpoint()
        txn, created = store.backfill_transaction(
            tid,
            Side.BAP,
            REFERRAL,
            "referral_on_confirm",
            status="ON_CONFIRM",
            counterparty_id=cc.id,
            counterparty_uri=cc.uri,
            contract_id=str((blob.get("contract") or {}).get("id") or ""),
            coordination_id=tid,
            patient_id=rr.related_patient_id,
            facility_id=rr.origin_facility_id,
        )
        linked = self.link_referral(txn, rr, ReferralRole.ORIGIN)
        self.outcome("referral_on_confirm", created or linked)

    def sent_referrals(self) -> None:
        if not self.since:
            self.count("referral_sent", "skipped")
            return
        qs = (
            ResourceRequest.objects.exclude(extensions__has_key="beckn")
            .filter(
                category__in=plugin_settings.BECKN_REFERRAL_CATEGORIES,
                status__in=OPEN_REFERRAL_STATUSES,
                created_date__gte=self.since,
            )
            .order_by("pk")
        )
        self.batched(qs.iterator(chunk_size=BATCH), self.sent_referral)

    def sent_referral(self, rr: ResourceRequest) -> None:
        if ReferralLink.objects.filter(resource_request=rr).exists():
            self.count("referral_sent", "existing")
            return
        cc = cc_endpoint()
        tid = str(rr.external_id)
        txn, created = store.backfill_transaction(
            tid,
            Side.BAP,
            REFERRAL,
            "referral_sent",
            status="ON_CONFIRM" if rr.status == "approved" else "CONFIRM",
            counterparty_id=cc.id,
            counterparty_uri=cc.uri,
            coordination_id=tid,
            patient_id=rr.related_patient_id,
            facility_id=rr.origin_facility_id,
        )
        linked = self.link_referral(txn, rr, ReferralRole.ORIGIN)
        self.outcome("referral_sent", created or linked)

    @staticmethod
    def link_referral(txn, rr, role: str) -> bool:
        if ReferralLink.objects.filter(transaction=txn, role=role, active=True).exists():
            return False
        ReferralLink.objects.create(transaction=txn, resource_request=rr, role=role, beckn_ref=txn.coordination_id)
        return True

    # ------------------------------------------------------------------------------------ appointments (5)
    def appointments(self) -> None:
        bookings = TokenBooking.objects.filter(meta__has_key="beckn")
        if self.since:
            bookings = bookings.filter(created_date__gte=self.since)
        groups: dict[str, list[TokenBooking]] = defaultdict(list)
        for booking in bookings.order_by("created_date").iterator(chunk_size=BATCH):
            tid = (booking.meta.get("beckn") or {}).get("transactionId")
            if tid:
                groups[tid].append(booking)
            else:
                self.count("appointment_booking", "skipped")
        self.batched(groups.items(), lambda item: self.appointment_group(*item))

    def appointment_group(self, tid: str, bookings: list[TokenBooking]) -> None:
        live = [b for b in bookings if b.status != "rescheduled"]
        chosen = live[-1] if live else None
        reference = chosen or bookings[-1]
        blob = reference.meta["beckn"]
        context = blob.get("context") or {}
        message = blob.get("message") or {}
        txn, created = store.backfill_transaction(
            tid,
            Side.BPP,
            APPOINTMENT,
            "appointment_booking",
            status="ON_CONFIRM",
            network_id=context.get("networkId") or "",
            counterparty_id=blob.get("bapId") or context.get("bapId") or "",
            counterparty_uri=blob.get("bapUri") or context.get("bapUri") or "",
            contract_id=str(reference.external_id),
            coordination_id=blob.get("coordinationRef") or find_key(message, "coordinationRef") or "",
            patient_id=reference.patient_id,
            facility_id=getattr(getattr(reference.token_slot, "resource", None), "facility_id", None),
        )
        changed = created
        if context.get("messageId") or message:
            payload = {"context": context, "message": message}
            _, msg_created = store.backfill_message(
                txn,
                context.get("messageId") or f"legacy-{tid}",
                "confirm",
                Direction.IN,
                payload,
                sender_id=store.inbound_sender(context, Side.BPP, payload) or blob.get("bapId", ""),
                processing_status=Processing.PROCESSED,
            )
            changed = changed or msg_created
        has_active = AppointmentLink.objects.filter(transaction=txn, active=True).exists()
        for booking in bookings:
            if AppointmentLink.objects.filter(transaction=txn, token_booking=booking).exists():
                continue
            active = booking == chosen and not has_active
            AppointmentLink.objects.create(
                transaction=txn, token_booking=booking, active=active, beckn_ref=str(booking.external_id)
            )
            has_active = has_active or active
            changed = True
        self.outcome("appointment_booking", changed)

    # ------------------------------------------------------------------------------------ redis (6, 7)
    def redis_keys(self, pattern: str) -> Iterator[str]:
        iter_keys = getattr(cache, "iter_keys", None)
        if iter_keys is None:
            return iter(())
        return iter(iter_keys(pattern))

    def booking_links(self) -> None:
        self.batched(self.redis_keys(f"{BOOKING_PREFIX}*"), self.booking_link)

    def booking_link(self, key: str) -> None:
        coordination_id = cache.get(key)
        booking_id = key.removeprefix(BOOKING_PREFIX)
        link = (
            AppointmentLink.objects.filter(token_booking__external_id=booking_id).select_related("transaction").first()
        )
        if not (coordination_id and link):
            self.count("redis_booking_referral", "skipped")
        elif link.transaction.coordination_id:
            self.count("redis_booking_referral", "existing")
        else:
            store.update_transaction(link.transaction, coordination_id=coordination_id)
            self.count("redis_booking_referral", "created")

    def redis_transactions(self) -> None:
        keys = (k for k in self.redis_keys(f"{TXN_PREFIX}*") if ":" not in k.removeprefix(TXN_PREFIX))
        self.batched(keys, self.redis_transaction)

    def redis_transaction(self, key: str) -> None:
        record = cache.get(key)
        flow = SERVICE_TYPE_FLOWS.get((record or {}).get("serviceType", ""))
        if not (record and flow):
            self.count("redis_transaction", "skipped")
            return
        routing = record.get("routing") or {}
        txn, created = store.backfill_transaction(
            record["transactionId"],
            Side.BAP,
            flow,
            "redis_transaction",
            status=record.get("status") or "QUEUED",
            counterparty_id=routing.get("bppId", ""),
            counterparty_uri=routing.get("bppUri", ""),
            state={"context_overrides": record.get("context") or {}},
        )
        changed = created
        for action in record.get("actions") or []:
            payload = cache.get(f"{TXN_PREFIX}{record['transactionId']}:{action}")
            if not payload:
                continue
            name, callback = action.lower(), action.lower().startswith("on_")
            context = payload.get("context") or {}
            _, msg_created = store.backfill_message(
                txn,
                context.get("messageId") or f"legacy-{record['transactionId']}-{name}",
                name,
                Direction.IN if callback else Direction.OUT,
                payload,
                sender_id=(context.get("bppId") or "") if callback else "",
                delivery_status="" if callback else Delivery.ACKED,
                processing_status=Processing.PROCESSED if callback else "",
            )
            changed = changed or msg_created
        self.outcome("redis_transaction", changed)


def find_key(data: Any, key: str) -> Any:
    if isinstance(data, dict):
        if key in data:
            return data[key]
        values: Iterable[Any] = data.values()
    elif isinstance(data, list):
        values = data
    else:
        return None
    for value in values:
        found = find_key(value, key)
        if found:
            return found
    return None


# -------------------------------------------------------------------------------------------- strip legacy
def unlinked_legacy_rows() -> int:
    """Legacy blobs (shapes 1-5) that have no plugin link yet."""
    referrals = (
        ResourceRequest.objects.filter(extensions__has_key="beckn")
        .filter(extensions__beckn__has_any_keys=["role", "transactionId"])
        .exclude(pk__in=ReferralLink.objects.values("resource_request_id"))
        .count()
    )
    bookings = (
        TokenBooking.objects.filter(meta__has_key="beckn")
        .exclude(pk__in=AppointmentLink.objects.values("token_booking_id"))
        .count()
    )
    return referrals + bookings


def strip_legacy(dry_run: bool = False) -> dict[str, int]:
    """Reduce the legacy blobs to what the plugin still reads. ``update()`` fires no signals."""
    counts = {"resource_request": 0, "patient": 0, "token_booking": 0}
    with transaction.atomic():
        for rr in ResourceRequest.objects.filter(extensions__has_key="beckn").iterator(chunk_size=BATCH):
            old = rr.extensions["beckn"] or {}
            new = {"v": 1, **({"target": old["target"]} if old.get("target") else {})}
            if old != new:
                ResourceRequest.objects.filter(pk=rr.pk).update(extensions={**rr.extensions, "beckn": new})
                counts["resource_request"] += 1
        for patient in Patient.objects.filter(extensions__has_key="beckn").iterator(chunk_size=BATCH):
            old = patient.extensions["beckn"] or {}
            language = old.get("primaryLanguage")
            new = {"v": 1, **({"primaryLanguage": language} if language else {})}
            if old != new:
                Patient.objects.filter(pk=patient.pk).update(extensions={**patient.extensions, "beckn": new})
                counts["patient"] += 1
        for booking in TokenBooking.objects.filter(meta__has_key="beckn").iterator(chunk_size=BATCH):
            meta = {k: v for k, v in booking.meta.items() if k != "beckn"}
            TokenBooking.objects.filter(pk=booking.pk).update(meta=meta)
            counts["token_booking"] += 1
        if dry_run:
            transaction.set_rollback(True)
    return counts
