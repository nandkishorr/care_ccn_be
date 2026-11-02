"""Care as the coordinating centre (BPP): receives referrals from the network and tracks them as Care referrals."""

from typing import Any

from care_ccn_be.bpp.mixins import BppConfirmMixin, BppInitMixin, BppSelectMixin, BppStatusMixin
from care_ccn_be.bpp.service import BppCall, BppServiceBase
from care_ccn_be.exceptions import BecknError
from care_ccn_be.flows.referral import lifecycle
from care_ccn_be.flows.referral.builders import ReferralEcho
from care_ccn_be.identity import cc_endpoint
from care_ccn_be.services import availability as availabilities
from care_ccn_be.services import facility as facilities
from care_ccn_be.services import mappers
from care_ccn_be.services.patient import find_or_create_patient
from care_ccn_be.services.users import system_user

INVALID_PAYLOAD = "70002"
NOT_FOUND = "70004"


def facility_candidates(call: BppCall) -> tuple[list, list]:
    attributes = mappers.get_contract_attributes(call.message)
    context = call.context
    origin = [context.get("facilityId"), attributes.get("facilityId"), attributes.get("originFacilityId")]
    assigned = [
        context.get("assignedFacilityId"),
        attributes.get("assignedFacilityId"),
        attributes.get("targetFacilityId"),
    ]
    return origin, assigned


class ReferralBppService(BppSelectMixin, BppInitMixin, BppConfirmMixin, BppStatusMixin, BppServiceBase):
    contract_builder = ReferralEcho

    def bpp_select(self, call: BppCall) -> dict[str, Any]:
        return ReferralEcho.on_select(call.message)

    def bpp_init(self, call: BppCall) -> dict[str, Any]:
        origin, assigned = self._facilities(call)
        facility = origin or assigned
        if facility is None:
            raise BecknError(INVALID_PAYLOAD, "No facility id in payload; cannot create the referral")
        role = lifecycle.ORIGIN if origin else lifecycle.ASSIGNED
        referral = self._ensure(call, role, origin, assigned, approved=False)
        return ReferralEcho.on_init(call.message, referral, self._details(call, role))

    def bpp_confirm(self, call: BppCall) -> dict[str, Any]:
        origin, assigned = self._facilities(call)
        if origin is None and assigned is None:
            raise BecknError(INVALID_PAYLOAD, "No facility id in payload; cannot confirm the referral")
        if origin is not None:
            self._ensure(call, lifecycle.ORIGIN, origin, assigned, approved=True)
        if assigned is not None:
            self._ensure(call, lifecycle.ASSIGNED, origin, assigned, approved=True)
        link = lifecycle.current_link(lifecycle.coordination_of(call.txn))
        return ReferralEcho.on_confirm(call.message, link.referral, link.details)

    def bpp_status(self, call: BppCall) -> dict[str, Any]:
        link = lifecycle.current_link(lifecycle.coordination_of(call.txn))
        if link is None:
            raise BecknError(NOT_FOUND, "Referral not found for status")
        return ReferralEcho.on_status(call.message, link.referral, link.details)

    @staticmethod
    def _facilities(call: BppCall):
        origin, assigned = facility_candidates(call)
        return facilities.resolve(origin), facilities.resolve(assigned)

    @staticmethod
    def _details(call: BppCall, role: str) -> dict[str, Any]:
        link = lifecycle.current_link(lifecycle.coordination_of(call.txn), role)
        return link.details if link else {}

    @staticmethod
    def _ensure(call: BppCall, role: str, origin, assigned, approved: bool):
        """Idempotent on (coordination id, role): an existing referral is reused, never duplicated.

        The origin referral sits on the network availability of the coordination centre (the referral the
        referring side raised); the assigned one sits on the receiving facility's availability.
        """
        coordination_id = lifecycle.coordination_of(call.txn)
        existing = lifecycle.current(coordination_id, role)
        if existing is not None:
            return lifecycle.approve(existing) if approved else existing
        facility = origin or assigned
        participant = mappers.find_patient_participant(call.message)
        patient = find_or_create_patient(
            mappers.get_contract_attributes(call.message), participant, facility, system_user()
        )
        if patient is None:
            raise BecknError(INVALID_PAYLOAD, "No patient participant in payload; cannot create the referral")
        referral_type = mappers.referral_type(call.message)
        if role == lifecycle.ASSIGNED:
            availability = availabilities.for_facility(assigned or facility, referral_type)
        else:
            cc = cc_endpoint()
            availability = availabilities.network(
                cc.id, cc.uri, referral_type, str(assigned.external_id) if assigned else None
            )
        data = lifecycle.ReferralData.from_message(
            call.message,
            availability,
            patient,
            origin_facility_id=str(facility.external_id),
            assignees=availabilities.assignees(availability) if role == lifecycle.ASSIGNED else (),
        )
        return lifecycle.ensure_referral(call.txn, role, data, approved=approved)
