"""One mixin per Beckn action. Mixing one into a ``BapViewSetBase`` subclass exposes ``POST <action>/``.

Each mixin routes to ``bap_<action>(source)``, which builds the request ``message``. Discover has a working default;
the rest must be implemented by the flow.
"""

from rest_framework.decorators import action

from care_ccn_be.bap.specs import BapSource
from care_ccn_be.exceptions import BecknError
from care_ccn_be.protocol.discover import build_discover_intent


def _missing(name: str):
    raise BecknError("70003", f"{name} is not implemented for this flow")


class BapDiscoverMixin:
    discover_filter_keys: tuple[str, ...] = ("healthServiceType", "acceptanceMode")

    @action(detail=False, methods=["post"], url_path="discover")
    def discover(self, request):
        return self.run("discover", request)

    def bap_discover(self, source: BapSource):
        return {"intent": build_discover_intent(source.spec.intent_query(), self.discover_filter_keys)}


class BapSelectMixin:
    @action(detail=True, methods=["post"], url_path="select")
    def select(self, request, transaction_id=None):
        return self.run("select", request, transaction_id)

    def bap_select(self, source: BapSource):
        _missing("select")


class BapInitMixin:
    @action(detail=True, methods=["post"], url_path="init")
    def init(self, request, transaction_id=None):
        return self.run("init", request, transaction_id)

    def bap_init(self, source: BapSource):
        _missing("init")


class BapConfirmMixin:
    @action(detail=True, methods=["post"], url_path="confirm")
    def confirm(self, request, transaction_id=None):
        return self.run("confirm", request, transaction_id)

    def bap_confirm(self, source: BapSource):
        _missing("confirm")


class BapStatusMixin:
    @action(detail=True, methods=["post"], url_path="status")
    def status(self, request, transaction_id=None):
        return self.run("status", request, transaction_id)

    def bap_status(self, source: BapSource):
        _missing("status")


class BapUpdateMixin:
    # not named ``update``: DRF reserves that name for the router's PUT route
    @action(detail=True, methods=["post"], url_path="update", url_name="update")
    def update_contract(self, request, transaction_id=None):
        return self.run("update", request, transaction_id)

    def bap_update(self, source: BapSource):
        _missing("update")


class BapCancelMixin:
    @action(detail=True, methods=["post"], url_path="cancel")
    def cancel(self, request, transaction_id=None):
        return self.run("cancel", request, transaction_id)

    def bap_cancel(self, source: BapSource):
        _missing("cancel")
