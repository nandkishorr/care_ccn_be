"""Flow viewsets by flow name; ``adapters`` on the viewset decides which adapters it serves."""

from care_ccn_be.api.viewsets.appointment import AppointmentViewSet
from care_ccn_be.api.viewsets.referral import ReferralViewSet
from care_ccn_be.flows.lookup import find_flow

VIEWSETS: dict[str, type] = {viewset.flow.name: viewset for viewset in (AppointmentViewSet, ReferralViewSet)}


def get_viewset(adapter: str, name: str) -> type | None:
    flow = find_flow(name)
    viewset = VIEWSETS.get(flow.name) if flow else None
    if viewset is None or adapter not in (viewset.adapters or (viewset.adapter,)):
        return None
    return viewset
