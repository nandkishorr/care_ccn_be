DEFAULT_SERVICE_TYPE = "PHYSICAL_CONSULTATION"
HEALTH_SERVICE_LAB_TEST = "LAB_TEST"
HEALTH_SERVICE_PHYSICAL_CONSULTATION = "PHYSICAL_CONSULTATION"

ACCEPTANCE_MODE_AUTO = "AUTO"
ACCEPTANCE_MODE_MANUAL_REVIEW = "MANUAL_REVIEW"

HEALTH_RESOURCE_CONTEXT = "https://schema.beckn.io/HealthResource/v2.1/context.jsonld"
HEALTH_OFFER_CONTEXT = "https://schema.beckn.io/HealthOffer/v2.1/context.jsonld"
CODED_VALUE_CONTEXT = "https://raw.githubusercontent.com/beckn/DHP-Specs/main/devkit/stub/context.jsonld"

DAY_OF_WEEK_NAMES = ("MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY")

COMPLETED_BOOKING_STATUSES = frozenset({"fulfilled", "checked_in", "in_consultation"})
CANCELLED_BOOKING_STATUSES = frozenset({"cancelled", "entered_in_error", "rescheduled"})
# held for a human care-coordinator review (acceptanceMode MANUAL_REVIEW); reported as DRAFT until approved
PENDING_BOOKING_STATUSES = frozenset({"pending", "proposed", "waitlist"})

NOTIFY_BOOKING_STATUSES = frozenset({"booked", "cancelled", "entered_in_error", "rescheduled", "fulfilled"})
