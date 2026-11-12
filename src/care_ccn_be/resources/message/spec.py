"""EMR read spec of a ``BecknMessage``: one request or callback, as stored."""

import datetime
from typing import Any

from care.emr.resources.base import EMRResource

from care_ccn_be.models import BecknMessage


class BecknMessageReadSpec(EMRResource):
    __model__ = BecknMessage
    __exclude__ = ["sender_id"]

    id: int | None = None
    transaction_id: str | None = None
    message_id: str = ""
    action: str = ""
    direction: str = ""
    delivery_status: str = ""
    processing_status: str = ""
    attempts: int = 0
    last_error: str = ""
    payload: dict[str, Any] = {}
    ack: dict[str, Any] | None = None
    reply_to: int | None = None
    created_date: datetime.datetime | None = None
    modified_date: datetime.datetime | None = None

    @classmethod
    def perform_extra_serialization(cls, mapping, obj, *args, **kwargs):
        # BecknMessage has no external_id: the primary key is its id
        mapping["id"] = obj.pk
        mapping["transaction_id"] = obj.transaction.transaction_id if obj.transaction_id else None
        mapping["reply_to"] = obj.reply_to_id
