"""Flow hooks run after an inbound callback has been stored and applied. Methods are named ``on_<action>``."""

from dataclasses import dataclass

from care_ccn_be.models import BecknMessage, BecknTransaction


@dataclass
class CallbackCall:
    flow: type
    txn: BecknTransaction
    msg: BecknMessage

    @property
    def message(self) -> dict:
        return self.msg.payload.get("message") or {}

    @property
    def error(self) -> dict | None:
        return self.msg.payload.get("error")


class BapCallbacksBase:
    def __init__(self, flow):
        self.flow = flow

    def handle(self, txn: BecknTransaction, msg: BecknMessage) -> None:
        handler = getattr(self, msg.action, None)
        if callable(handler):
            handler(CallbackCall(self.flow, txn, msg))
