"""Flows by name. ``BecknTransaction.flow`` and ``BECKN_ENABLED_FLOWS`` store names; a first BPP request has
neither, so it is matched against the enabled flows."""

from operator import itemgetter

from care_ccn_be.flows.appointment import AppointmentFlow
from care_ccn_be.flows.base import BecknFlow
from care_ccn_be.flows.referral import ReferralFlow
from care_ccn_be.settings import plugin_settings

FLOWS: dict[str, type[BecknFlow]] = {flow.name: flow for flow in (AppointmentFlow, ReferralFlow)}


def find_flow(name: str) -> type[BecknFlow] | None:
    return FLOWS.get(name) or next((flow for flow in FLOWS.values() if name in flow.aliases), None)


def get_flow(name: str) -> type[BecknFlow]:
    flow = find_flow(name)
    if flow is None:
        raise KeyError(name)
    return flow


def enabled_flows() -> list[type[BecknFlow]]:
    return [FLOWS[name] for name in plugin_settings.BECKN_ENABLED_FLOWS if name in FLOWS]


def is_flow_enabled(name: str) -> bool:
    flow = find_flow(name)
    return flow is not None and flow in enabled_flows()


def match_flow(request) -> type[BecknFlow] | None:
    score, flow = max(((f.match(request), f) for f in enabled_flows()), key=itemgetter(0), default=(0, None))
    return flow if score > 0 else None
