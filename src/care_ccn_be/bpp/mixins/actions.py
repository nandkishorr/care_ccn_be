"""One mixin per Beckn action. Mixing one into a ``BppServiceBase`` subclass is what makes the BPP accept it.

select/init/confirm/cancel default to echoing the contract with the matching status; discover builds the
flow's catalog; status and update have no sensible default and must be overridden.
"""

from care_ccn_be.exceptions import BecknError
from care_ccn_be.protocol.actions import CONTRACT_ACTIVE, CONTRACT_CANCELLED, CONTRACT_DRAFT

NOT_IMPLEMENTED = "70003"


class BppDiscoverMixin:
    def bpp_discover(self, call):
        if self.flow.catalog_builder is None:
            raise BecknError(NOT_IMPLEMENTED, f"{self.flow.name} publishes no catalog")
        return {"catalogs": [self.flow.catalog_builder().build(call)]}


class BppSelectMixin:
    def bpp_select(self, call):
        return self.contract_builder.echo(call.message, CONTRACT_DRAFT)


class BppInitMixin:
    def bpp_init(self, call):
        return self.contract_builder.echo(call.message, CONTRACT_DRAFT)


class BppConfirmMixin:
    def bpp_confirm(self, call):
        return self.contract_builder.echo(call.message, CONTRACT_ACTIVE)


class BppCancelMixin:
    def bpp_cancel(self, call):
        return self.contract_builder.echo(call.message, CONTRACT_CANCELLED)


class BppStatusMixin:
    def bpp_status(self, call):
        raise BecknError(NOT_IMPLEMENTED, f"{self.flow.name} does not implement status")


class BppUpdateMixin:
    def bpp_update(self, call):
        raise BecknError(NOT_IMPLEMENTED, f"{self.flow.name} does not implement update")
