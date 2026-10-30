"""Typed request bodies of the referral BAP endpoints (camelCase on the wire, snake_case accepted)."""

from pydantic import model_validator

from care_ccn_be.bap.specs import BapSpec
from care_ccn_be.flows.base import FlowState

OFFER_REQUIRED = "offerId is required to select a coordinator"
FACILITY_REQUIRED = "facility is required (or contractAttributes.facilityId in a passthrough message)"


class ReferralState(FlowState):
    offer_id: str | None = None
    init: dict | None = None
    confirm: dict | None = None


class ReferralSelectSpec(BapSpec):
    offer_id: str | None = None
    contract: dict | None = None  # a contract fragment: the offer is ``contract.offers[0].id``

    @model_validator(mode="after")
    def _offer(self):
        if self.message is None and not self.offer_id:
            offers = (self.contract or {}).get("offers") or []
            self.offer_id = (offers[0] or {}).get("id") if offers else None
            if not self.offer_id:
                raise ValueError(OFFER_REQUIRED)
        return self


class ReferralRequestSpec(BapSpec):
    facility: str | None = None
    patient: str | None = None
    title: str | None = None
    reason: str | None = None
    category: str | None = None
    offer_id: str | None = None
    assigned_facility_id: str | None = None
    emergency: bool = False
    priority: int | None = None
    patient_name: str | None = None

    @model_validator(mode="after")
    def _facility(self):
        if self.message is None and not self.facility:
            raise ValueError(FACILITY_REQUIRED)
        return self

    def params(self) -> dict:
        """The parameters kept in ``state`` so the ``on_confirm`` callback can create the origin request."""
        return self.model_dump(exclude={"context", "message"}, exclude_none=True)


class ReferralInitSpec(ReferralRequestSpec):
    pass


class ReferralConfirmSpec(ReferralRequestSpec):
    pass
