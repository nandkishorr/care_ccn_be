"""EMR list API of the stored Beckn messages (requests and callbacks) of one transaction.

``GET bap/message/?transaction_id=<id>[&action=on_select][&direction=in]`` (same for ``bpp/message/``).
"""

from typing import ClassVar

from care.emr.api.viewsets.base import EMRBaseViewSet, EMRListMixin
from django_filters import rest_framework as filters
from rest_framework.exceptions import ValidationError

from care_ccn_be.api.viewsets.base import BecknAutoSchema, beckn_exception_handler
from care_ccn_be.models import Adapter, BecknMessage
from care_ccn_be.resources.message.spec import BecknMessageReadSpec


class BecknMessageFilters(filters.FilterSet):
    transaction_id = filters.CharFilter(field_name="transaction__transaction_id")
    flow = filters.CharFilter(field_name="transaction__flow")
    action = filters.CharFilter(field_name="action", lookup_expr="iexact")
    direction = filters.CharFilter(field_name="direction", lookup_expr="iexact")


class BecknMessageViewSet(EMRListMixin, EMRBaseViewSet):
    adapter: ClassVar[str]

    database_model = BecknMessage
    pydantic_read_model = BecknMessageReadSpec
    filter_backends = [filters.DjangoFilterBackend]
    filterset_class = BecknMessageFilters
    schema = BecknAutoSchema()

    def get_exception_handler(self):
        return beckn_exception_handler

    def get_queryset(self):
        """Permission hook (later): narrow to the transactions a user may see."""
        if not self.request.query_params.get("transaction_id"):
            raise ValidationError("transaction_id is required")
        return (
            BecknMessage.objects.filter(transaction__adapter=self.adapter).select_related("transaction").order_by("pk")
        )


class BapMessageViewSet(BecknMessageViewSet):
    adapter = Adapter.BAP.value


class BppMessageViewSet(BecknMessageViewSet):
    adapter = Adapter.BPP.value
