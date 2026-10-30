"""Wrappers over Care's scheduling system, so the appointment flow never goes through DRF or imports Care models."""

import datetime
import uuid

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from care_ccn_be.settings import plugin_settings


def valid_uuid(value) -> bool:
    try:
        uuid.UUID(str(value))
    except ValueError:
        return False
    return True


def system_user():
    from care.users.models import User

    username = plugin_settings.BECKN_SYSTEM_USERNAME
    return User.objects.filter(username=username).first() if username else None


def get_resource(external_id):
    from care.emr.models.scheduling.schedule import SchedulableResource

    if not external_id or not valid_uuid(external_id):
        return None
    return SchedulableResource.objects.select_related("facility").filter(external_id=external_id).first()


def resource_name(resource) -> str:
    from care.emr.resources.scheduling.schedule.spec import SchedulableResourceTypeOptions as Kind

    if resource.resource_type == Kind.practitioner.value and resource.user:
        return resource.user.get_full_name() or resource.user.username
    if resource.resource_type == Kind.healthcare_service.value and resource.healthcare_service:
        return resource.healthcare_service.name
    if resource.resource_type == Kind.location.value and resource.location:
        return resource.location.name
    return resource.resource_type


def is_healthcare_service(resource) -> bool:
    from care.emr.resources.scheduling.schedule.spec import SchedulableResourceTypeOptions as Kind

    return resource.resource_type == Kind.healthcare_service.value


def beckn_meta(schedules, key: str):
    """First ``Schedule.meta["beckn"][key]`` set across the given schedules."""
    for schedule in schedules:
        value = ((schedule.meta or {}).get("beckn") or {}).get(key)
        if value:
            return value
    return None


def resource_schedules(resource):
    from care.emr.models.scheduling.schedule import Schedule

    return list(Schedule.objects.filter(resource=resource))


def availability_entries(schedules) -> list[dict]:
    from care.emr.models.scheduling.schedule import Availability
    from care.emr.resources.scheduling.schedule.spec import SlotTypeOptions

    entries = []
    for availability in Availability.objects.filter(
        schedule__in=schedules, slot_type=SlotTypeOptions.appointment.value
    ).order_by("pk"):
        entries.extend(availability.availability or [])
    return entries


def schedules_by_facility(public_only: bool = True) -> list[dict]:
    """``[{"facility": f, "resources": [{"resource": r, "schedules": [s, ...]}, ...]}, ...]`` in a stable order."""
    from care.emr.models.scheduling.schedule import Schedule

    schedules = Schedule.objects.select_related(
        "resource",
        "resource__facility",
        "resource__user",
        "resource__healthcare_service",
        "resource__location",
    ).order_by("pk")
    if public_only:
        schedules = schedules.filter(is_public=True)
    facilities: dict = {}
    for schedule in schedules:
        resource = schedule.resource
        bucket = facilities.setdefault(resource.facility_id, {"facility": resource.facility, "resources": {}})
        bucket["resources"].setdefault(resource.pk, {"resource": resource, "schedules": []})["schedules"].append(
            schedule
        )
    return [
        {"facility": item["facility"], "resources": list(item["resources"].values())} for item in facilities.values()
    ]


def list_slots_for_day(resource, day: datetime.date, *, public_only: bool = True) -> list:
    """Bookable ``TokenSlot`` rows of a resource on a day; like Care's slots-for-day API it creates missing slots."""
    from care.emr.api.viewsets.scheduling.availability import convert_availability_and_exceptions_to_slots
    from care.emr.models.scheduling.booking import TokenSlot
    from care.emr.models.scheduling.schedule import Availability, AvailabilityException
    from care.emr.resources.scheduling.schedule.spec import SlotTypeOptions

    availabilities = Availability.objects.filter(
        slot_type=SlotTypeOptions.appointment.value,
        schedule__valid_from__lte=day,
        schedule__valid_to__gte=day,
        schedule__resource=resource,
    )
    if public_only:
        availabilities = availabilities.filter(schedule__is_public=True)

    day_availabilities = [
        {
            "availability": entry,
            "slot_size_in_minutes": availability.slot_size_in_minutes,
            "availability_id": availability.id,
        }
        for availability in availabilities
        for entry in availability.availability
        if entry["day_of_week"] == day.weekday()
    ]
    exceptions = AvailabilityException.objects.filter(resource=resource, valid_from__lte=day, valid_to__gte=day)
    candidates = convert_availability_and_exceptions_to_slots(day_availabilities, exceptions, day)

    existing = TokenSlot.objects.filter(start_datetime__date=day, end_datetime__date=day, resource=resource)
    if public_only:
        existing = existing.filter(availability__schedule__is_public=True)
    for slot in existing:
        key = (
            f"{slot.availability_id}-{timezone.make_naive(slot.start_datetime).time()}-"
            f"{timezone.make_naive(slot.end_datetime).time()}"
        )
        if key in candidates and candidates[key]["availability_id"] == slot.availability_id:
            candidates.pop(key)

    now = timezone.make_naive(timezone.now())
    for candidate in candidates.values():
        end = datetime.datetime.combine(day, candidate["end_time"], tzinfo=None)
        if end < now:
            continue
        TokenSlot.objects.create(
            resource=resource,
            start_datetime=datetime.datetime.combine(day, candidate["start_time"], tzinfo=None),
            end_datetime=end,
            availability_id=candidate["availability_id"],
        )

    slots = TokenSlot.objects.filter(
        start_datetime__date=day, end_datetime__date=day, resource=resource
    ).select_related("availability", "availability__schedule")
    if public_only:
        slots = slots.filter(availability__schedule__is_public=True)
    slots = slots.filter(allocated__lt=F("availability__tokens_per_slot"))
    return list(slots.order_by("start_datetime"))


def get_slot(slot_id):
    from care.emr.models.scheduling.booking import TokenSlot

    if not slot_id or not valid_uuid(slot_id):
        return None
    return TokenSlot.objects.select_related("resource", "resource__facility").filter(external_id=slot_id).first()


def book(token_slot, patient, user, note: str = ""):
    from care.emr.api.viewsets.scheduling.availability import lock_create_appointment

    return lock_create_appointment(token_slot, patient, user, note or "")


def hold_for_review(booking):
    from care.emr.resources.scheduling.slot.spec import BookingStatusChoices

    booking.status = BookingStatusChoices.pending.value
    booking.save(update_fields=["status", "modified_date"])
    return booking


def approve(booking):
    from care.emr.resources.scheduling.slot.spec import BookingStatusChoices

    booking.status = BookingStatusChoices.booked.value
    booking.save(update_fields=["status", "modified_date"])
    return booking


def cancel(booking, user, reason: str = "cancelled", note: str | None = None):
    from care.emr.api.viewsets.scheduling.booking import TokenBookingViewSet

    return TokenBookingViewSet.cancel_appointment_handler(booking, {"reason": reason, "note": note}, user)


def reschedule(booking, new_slot, user, note: str = ""):
    """Cancel ``booking`` as rescheduled and book ``new_slot`` for the same patient, atomically."""
    from care.emr.resources.scheduling.slot.spec import BookingStatusChoices

    with transaction.atomic():
        cancel(booking, user, BookingStatusChoices.rescheduled.value, note or booking.note)
        return book(new_slot, booking.patient, user, note)


def find_booking(external_id):
    from care.emr.models.scheduling.booking import TokenBooking

    if not external_id or not valid_uuid(external_id):
        return None
    return (
        TokenBooking.objects.filter(external_id=external_id)
        .select_related("token_slot", "token_slot__resource", "token_slot__resource__facility", "patient")
        .first()
    )


def ensure_token(booking, user):
    """Best-effort queue token for a confirmed booking; ``None`` when the facility has no category for the resource."""
    from care.emr.models.scheduling.token import Token, TokenCategory, TokenQueue
    from care.emr.resources.scheduling.token.spec import TokenStatusOptions
    from care.utils.lock import Lock

    if booking.token_id:
        return booking.token
    resource = booking.token_slot.resource
    facility = resource.facility
    category = TokenCategory.objects.filter(facility=facility, resource_type=resource.resource_type).first()
    if category is None:
        return None

    token_date = timezone.make_naive(booking.token_slot.start_datetime + datetime.timedelta(seconds=1)).date()
    filters = {"facility": facility, "resource": resource, "date": token_date}
    queue_exists = TokenQueue.objects.filter(**filters).exists()
    filters["system_generated"] = True
    queue = TokenQueue.objects.filter(**filters).first()
    if queue is None:
        filters["name"] = "System Generated"
        filters["is_primary"] = not queue_exists
        queue = TokenQueue.objects.create(**filters)

    with Lock(f"booking:token:{queue.id}"), transaction.atomic():
        token = Token.objects.create(
            facility=facility,
            queue=queue,
            category=category,
            number=Token.objects.filter(queue=queue, category=category).count() + 1,
            status=TokenStatusOptions.CREATED.value,
            booking=booking,
            patient=booking.patient,
        )
        booking.token = token
        booking.save(update_fields=["token", "modified_date"])
    return token
