from django.http import HttpResponse
from django.urls import path, re_path
from django.utils.module_loading import import_string
from rest_framework.routers import SimpleRouter

from care_ccn_be.bap.receiver import BapReceiverView
from care_ccn_be.bpp.receiver import BppReceiverView
from care_ccn_be.flows.registry import FlowRegistry


def healthy(request):
    return HttpResponse("OK")


def build_urlpatterns(flows):
    router = SimpleRouter()
    for flow in flows:
        if flow.bap_viewset:
            router.register(
                f"bap/{flow.name}", import_string(flow.bap_viewset), basename=f"care_ccn_be-bap-{flow.name}"
            )
    return [
        path("health", healthy, name="care_ccn_be-health"),
        re_path(
            r"^bap/receiver(?:/(?P<action>on_[a-z_]+))?/?$", BapReceiverView.as_view(), name="care_ccn_be-bap-receiver"
        ),
        re_path(
            r"^bpp/receiver(?:/(?P<action>[a-z_]+))?/?$", BppReceiverView.as_view(), name="care_ccn_be-bpp-receiver"
        ),
        *router.urls,
    ]


urlpatterns = build_urlpatterns(FlowRegistry.enabled())
