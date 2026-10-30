"""The one path every BAP request takes: lock, context, build, persist, queue delivery."""

from care_ccn_be import store
from care_ccn_be.bap.specs import BapSource
from care_ccn_be.models import BecknMessage, BecknTransaction
from care_ccn_be.protocol.actions import DISCOVER


def dispatch_beckn_action(viewset, action: str, txn: BecknTransaction, spec) -> BecknMessage:
    """Must run inside ``transaction.atomic()``; delivery is queued ``on_commit``."""
    flow = viewset.flow
    txn = store.lock(txn)
    state = store.remember_overrides(txn, flow, action, spec.context)
    routing = {} if action == DISCOVER else {"bppId": txn.counterparty_id, "bppUri": txn.counterparty_uri}
    context = flow.context().request(action, txn.transaction_id, routing, state.context_overrides)
    source = BapSource(flow, spec, txn, state)
    if spec.message is not None:
        message = viewset.validate_passthrough(action, spec.message)
    else:
        message = getattr(viewset, f"bap_{action}")(source)
    msg = store.record_outbound(txn, context, message)
    viewset.after_send(action, source, msg)
    store.enqueue_delivery(msg)
    return msg
