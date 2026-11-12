"""Shared vocabulary and booking constants used by the flows."""

from enum import Enum

from care_ccn_be.resources.beckn.codes import DayOfWeek, HealthServiceType
from care_ccn_be.resources.beckn.health import CODED_VALUE_CONTEXT as _CODED_VALUE_CONTEXT


class AcceptanceMode(str, Enum):
    AUTO = "AUTO"
    MANUAL_REVIEW = "MANUAL_REVIEW"


class ContextUrl(str, Enum):
    HEALTH_RESOURCE = "https://schema.beckn.io/HealthResource/v2.1/context.jsonld"
    HEALTH_OFFER = "https://schema.beckn.io/HealthOffer/v2.1/context.jsonld"
    CODED_VALUE = _CODED_VALUE_CONTEXT


class BookingStatus(str, Enum):
    """Care ``TokenBooking.status`` values (lowercase, as stored by Care)."""

    FULFILLED = "fulfilled"
    CHECKED_IN = "checked_in"
    IN_CONSULTATION = "in_consultation"
    CANCELLED = "cancelled"
    ENTERED_IN_ERROR = "entered_in_error"
    RESCHEDULED = "rescheduled"
    PENDING = "pending"
    PROPOSED = "proposed"
    WAITLIST = "waitlist"
    BOOKED = "booked"


DEFAULT_SERVICE_TYPE = HealthServiceType.PHYSICAL_CONSULTATION
HEALTH_SERVICE_LAB_TEST = HealthServiceType.LAB_TEST
HEALTH_SERVICE_PHYSICAL_CONSULTATION = HealthServiceType.PHYSICAL_CONSULTATION

ACCEPTANCE_MODE_AUTO = AcceptanceMode.AUTO
ACCEPTANCE_MODE_MANUAL_REVIEW = AcceptanceMode.MANUAL_REVIEW

HEALTH_RESOURCE_CONTEXT = ContextUrl.HEALTH_RESOURCE
HEALTH_OFFER_CONTEXT = ContextUrl.HEALTH_OFFER
CODED_VALUE_CONTEXT = ContextUrl.CODED_VALUE

DAY_OF_WEEK_NAMES = tuple(DayOfWeek)

COMPLETED_BOOKING_STATUSES = [BookingStatus.FULFILLED, BookingStatus.CHECKED_IN, BookingStatus.IN_CONSULTATION]
CANCELLED_BOOKING_STATUSES = [BookingStatus.CANCELLED, BookingStatus.ENTERED_IN_ERROR, BookingStatus.RESCHEDULED]
# held for a human care-coordinator review (acceptanceMode MANUAL_REVIEW); reported as DRAFT until approved
PENDING_BOOKING_STATUSES = [BookingStatus.PENDING, BookingStatus.PROPOSED, BookingStatus.WAITLIST]

NOTIFY_BOOKING_STATUSES = [
    BookingStatus.BOOKED,
    BookingStatus.CANCELLED,
    BookingStatus.ENTERED_IN_ERROR,
    BookingStatus.RESCHEDULED,
    BookingStatus.FULFILLED,
]
