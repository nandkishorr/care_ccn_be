"""DHP v2.1 code lists (https://github.com/beckn/DHP-Specs/tree/main/devkit/config/schema).

Fields typed with ``Code[...]`` turn known values into the enum member and let unknown strings through
unchanged: the ONIX adapter is the one that rejects off-spec codes, Care only validates the shape.
"""

from enum import Enum
from typing import TYPE_CHECKING, Annotated, Any, TypeAlias, TypeVar

from pydantic import BeforeValidator


class CodeEnum(str, Enum):
    """A code that behaves exactly like its string value (``str()``, f-strings, JSON, ``==``)."""

    __str__ = str.__str__
    __format__ = str.__format__


class HealthServiceType(CodeEnum):
    DIAGNOSTIC_ANALYTICS = "DIAGNOSTIC_ANALYTICS"
    TELECONSULTATION = "TELECONSULTATION"
    PHYSICAL_CONSULTATION = "PHYSICAL_CONSULTATION"
    RADIOLOGY = "RADIOLOGY"
    LAB_TEST = "LAB_TEST"
    PHARMACY_DISPENSING = "PHARMACY_DISPENSING"


class ParticipantRole(CodeEnum):
    PATIENT = "PATIENT"
    CARE_GIVER = "CARE_GIVER"
    SPECIALIST = "SPECIALIST"
    GENERAL_PRACTITIONER = "GENERAL_PRACTITIONER"
    FIELD_WORKER = "FIELD_WORKER"
    PAYER = "PAYER"
    IMPLEMENTING_AGENCY = "IMPLEMENTING_AGENCY"
    CARE_COORDINATOR = "CARE_COORDINATOR"


class Gender(CodeEnum):
    MALE = "MALE"
    FEMALE = "FEMALE"
    OTHER = "OTHER"
    PREFER_NOT_TO_SAY = "PREFER_NOT_TO_SAY"


class UrgencyTier(CodeEnum):
    ROUTINE = "ROUTINE"
    URGENT = "URGENT"
    EMERGENCY = "EMERGENCY"


class ReferralLifecycleState(CodeEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    TARGET_SELECTED = "TARGET_SELECTED"
    BOOKING_CONFIRMED = "BOOKING_CONFIRMED"
    TARGET_NOTIFIED = "TARGET_NOTIFIED"
    ATTENDED = "ATTENDED"
    NO_SHOW = "NO_SHOW"
    CANCELLED = "CANCELLED"
    PROVIDER_CANCELLED = "PROVIDER_CANCELLED"
    OUTCOME_RECEIVED = "OUTCOME_RECEIVED"
    SLA_BREACHED = "SLA_BREACHED"
    LAPSED = "LAPSED"
    REVOKED = "REVOKED"
    WITHDRAWN = "WITHDRAWN"
    CLOSED = "CLOSED"
    SCHEME_PREAUTH_PENDING = "SCHEME_PREAUTH_PENDING"
    POST_FACTO_CONSENT_PENDING = "POST_FACTO_CONSENT_PENDING"


class ConsultationModality(CodeEnum):
    IN_PERSON = "IN_PERSON"
    TELECONSULT = "TELECONSULT"


class RevocationStatus(CodeEnum):
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"


class BookingChannel(CodeEnum):
    APP = "APP"
    VOICE = "VOICE"
    IN_PERSON = "IN_PERSON"
    USSD = "USSD"
    WHATSAPP = "WHATSAPP"
    SMS = "SMS"
    OTHER = "OTHER"


class ArtifactType(CodeEnum):
    AUDIO_SAMPLE = "AUDIO_SAMPLE"
    IMAGING_SCAN = "IMAGING_SCAN"
    LAB_REPORT = "LAB_REPORT"
    PRESCRIPTION = "PRESCRIPTION"
    REFERRAL = "REFERRAL"
    PHOTO = "PHOTO"
    OTHER = "OTHER"


class AttendanceStatus(CodeEnum):
    ATTENDED = "ATTENDED"
    NO_SHOW = "NO_SHOW"
    LEFT_WITHOUT_BEING_SEEN = "LEFT_WITHOUT_BEING_SEEN"


class FollowupRecommendation(CodeEnum):
    NONE = "NONE"
    ROUTINE = "ROUTINE"
    SOON = "SOON"
    URGENT = "URGENT"
    EMERGENCY = "EMERGENCY"


class OfferType(CodeEnum):
    SINGLE_EVENT = "SINGLE_EVENT"
    BULK_PROCUREMENT = "BULK_PROCUREMENT"
    CAMP = "CAMP"


class ClinicalDeliveryUnit(CodeEnum):
    ANALYSIS = "ANALYSIS"
    CONSULTATION = "CONSULTATION"
    SCREENING = "SCREENING"
    REPORT = "REPORT"
    SESSION = "SESSION"
    TEST = "TEST"


class ServiceUnit(CodeEnum):
    PER_HOUR = "PER_HOUR"
    PER_SESSION = "PER_SESSION"
    PER_VISIT = "PER_VISIT"
    PER_UNIT = "PER_UNIT"
    PER_KG = "PER_KG"
    PER_DAY = "PER_DAY"
    PER_PIECE = "PER_PIECE"
    OTHER = "OTHER"


class DayOfWeek(CodeEnum):
    MONDAY = "MONDAY"
    TUESDAY = "TUESDAY"
    WEDNESDAY = "WEDNESDAY"
    THURSDAY = "THURSDAY"
    FRIDAY = "FRIDAY"
    SATURDAY = "SATURDAY"
    SUNDAY = "SUNDAY"


class CoordinationAcceptanceMode(CodeEnum):
    AUTO_ACCEPT = "AUTO_ACCEPT"
    MANUAL_REVIEW = "MANUAL_REVIEW"


E = TypeVar("E", bound=CodeEnum)

if TYPE_CHECKING:
    Code: TypeAlias = E | str
else:

    class Code:
        """``Code[SomeEnum]``: the enum member for a known value, the plain string otherwise."""

        def __class_getitem__(cls, enum: type[CodeEnum]) -> Any:
            def coerce(value: Any) -> Any:
                if isinstance(value, str) and value in enum._value2member_map_:
                    return enum(value)
                return value

            return Annotated[enum | str, BeforeValidator(coerce)]
