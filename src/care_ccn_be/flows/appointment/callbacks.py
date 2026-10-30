from care_ccn_be import store
from care_ccn_be.bap.callbacks import BapCallbacksBase, CallbackCall


class AppointmentCallbacks(BapCallbacksBase):
    def on_select(self, call: CallbackCall) -> None:
        """Remember the offered slots so init/confirm/update can carry their windows."""
        slots = {}
        for performance in (call.message.get("contract") or {}).get("performance") or []:
            attributes = performance.get("performanceAttributes") or {}
            if performance.get("id"):
                slots[performance["id"]] = {
                    "start": attributes.get("appointmentWindowStart"),
                    "end": attributes.get("appointmentWindowEnd"),
                }
        if slots:
            state = store.get_state(call.txn, self.flow)
            store.update_state(call.txn, self.flow, slots={**state.slots, **slots})
