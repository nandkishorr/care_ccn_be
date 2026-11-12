from django.http import HttpResponse
from django.urls import path, re_path
from rest_framework.routers import SimpleRouter

from care_ccn_be.api.viewsets.lookup import get_viewset
from care_ccn_be.api.viewsets.message import BapMessageViewSet, BppMessageViewSet
from care_ccn_be.api.viewsets.receiver import BapReceiverViewSet, BppReceiverViewSet
from care_ccn_be.flows.lookup import enabled_flows
from care_ccn_be.models import Adapter

RECEIVE = {"post": "receive"}


class AdapterRouter(SimpleRouter):
    """Bind a shared flow viewset to an adapter without exposing BAP actions on BPP routes."""

    def __init__(self, adapter: str):
        super().__init__()
        self.adapter = adapter

    def get_routes(self, viewset):
        routes = []
        for route in super().get_routes(viewset):
            mapping = route.mapping
            if self.adapter == Adapter.BPP.value:
                mapping = {method: name for method, name in mapping.items() if name in {"list", "retrieve"}}
            if mapping:
                routes.append(route._replace(mapping=mapping, initkwargs={**route.initkwargs, "adapter": self.adapter}))
        return routes


def healthy(request):
    return HttpResponse("OK")


def build_urlpatterns(flows):
    """Routes of the given flows: every viewset that declares one of them (``bap/<flow>``, ``bpp/<flow>``)."""
    flow_urls = []
    for flow in flows:
        for adapter in (Adapter.BAP.value, Adapter.BPP.value):
            viewset = get_viewset(adapter, flow.name)
            if viewset is not None:
                router = AdapterRouter(adapter)
                router.register(f"{adapter}/{flow.name}", viewset, basename=f"care_ccn_be-{adapter}-{flow.name}")
                flow_urls.extend(router.urls)
    router = SimpleRouter()
    router.register("bap/message", BapMessageViewSet, basename="care_ccn_be-bap-message")
    router.register("bpp/message", BppMessageViewSet, basename="care_ccn_be-bpp-message")
    return [
        path("health", healthy, name="care_ccn_be-health"),
        re_path(
            r"^bap/receiver(?:/(?P<action>on_[a-z_]+))?/?$",
            BapReceiverViewSet.as_view(RECEIVE),
            name="care_ccn_be-bap-receiver",
        ),
        re_path(
            r"^bpp/receiver(?:/(?P<action>[a-z_]+))?/?$",
            BppReceiverViewSet.as_view(RECEIVE),
            name="care_ccn_be-bpp-receiver",
        ),
        *flow_urls,
        *router.urls,
    ]


urlpatterns = build_urlpatterns(enabled_flows())
