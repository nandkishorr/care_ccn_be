"""Request specs of the referral BAP APIs, the flow state and the referral transaction retrieve spec."""

from typing import Any

from pydantic import UUID4

from care_ccn_be.flows.base import FlowState
from care_ccn_be.resources.bap.spec import BapActionSpec
from care_ccn_be.resources.referral.schema import (
    FACILITY_REQUIRED,
    OFFER_REQUIRED,
    ReferralConfirmMessage,
    ReferralMessage,
    ReferralSelectMessage,
)
from care_ccn_be.resources.transaction.spec import BecknTransactionRetrieveSpec

__all__ = ["FACILITY_REQUIRED", "OFFER_REQUIRED"]


class ReferralState(FlowState):
    offer_id: str | None = None
    # the availability (and its form snapshots) the BPP returned on on_select
    availability: dict | None = None
    init: dict | None = None
    confirm: dict | None = None


class ReferralSelectSpec(BapActionSpec):
    __required__ = ("offer_id",)

    message: ReferralSelectMessage | None = None
    offer_id: str | None = None


class ReferralRequestSpec(BapActionSpec):
    __required__ = ("facility",)

    message: ReferralMessage | None = None
    facility: UUID4 | None = None
    patient: UUID4 | None = None
    patient_name: str | None = None
    # Care-local: the origin referral is linked to this encounter (and its patient); never sent to the network
    encounter: UUID4 | None = None
    title: str | None = None
    reason: str | None = None
    category: str | None = None
    offer_id: str | None = None
    assigned_facility_id: str | None = None
    emergency: bool = False
    priority: int | None = None
    # the answered request form of the selected availability (Care's QuestionnaireResponseSubmission shape)
    questionnaire: UUID4 | None = None
    questionnaire_response: dict[str, Any] | None = None


class ReferralStatusSpec(BapActionSpec):
    message: ReferralMessage | None = None


class ReferralInitSpec(ReferralRequestSpec):
    pass


class ReferralConfirmSpec(ReferralRequestSpec):
    message: ReferralConfirmMessage | None = None


class ReferralTransactionRetrieveSpec(BecknTransactionRetrieveSpec):
    referral_id: UUID4 | None = None
    availability: dict[str, Any] | None = None
    confirm: dict[str, Any] | None = None

    @classmethod
    def perform_extra_serialization(cls, mapping, obj, *args, **kwargs):
        from care_ccn_be.services import referral as lifecycle

        super().perform_extra_serialization(mapping, obj, *args, **kwargs)
        mapping["referral_id"] = lifecycle.origin_referral_id(obj)
        mapping["availability"] = (obj.state or {}).get("availability")
        mapping["confirm"] = (obj.state or {}).get("confirm")
