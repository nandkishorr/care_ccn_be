"""Read side: the FE poll and ``?action=on_x`` payload views."""

from typing import Any

from care_ccn_be import store
from care_ccn_be.models import BecknTransaction
from care_ccn_be.protocol.actions import callback_of


def routing_view(txn: BecknTransaction) -> dict[str, Any]:
    from care_ccn_be.identity import identity_from_settings

    identity = identity_from_settings()
    return {
        "bapId": identity.bap.id,
        "bapUri": identity.bap.uri,
        "bppId": txn.counterparty_id,
        "bppUri": txn.counterparty_uri,
    }


def poll(flow, txn: BecknTransaction) -> dict[str, Any]:
    state = txn.state or {}
    body = {
        "transactionId": txn.transaction_id,
        "serviceType": flow.legacy_service_type or flow.name,
        "flow": flow.name,
        "status": txn.status,
        "routing": routing_view(txn),
        "context": state.get("context_overrides") or {},
        "actions": store.actions_seen(txn),
        "patient": state.get("confirm"),
        "contractId": txn.contract_id or None,
        "error": txn.last_error,
    }
    body.update(flow.poll_extra(txn))
    return body


def action_view(txn: BecknTransaction, action: str) -> dict[str, Any]:
    action = action.lower()
    payload = store.latest_payload(txn, action)
    if payload is not None and action == callback_of("discover"):
        payload = {**payload, "message": {**(payload.get("message") or {}), "catalogs": store.merged_catalogs(txn)}}
    return {
        "transactionId": txn.transaction_id,
        "status": txn.status,
        "action": action.upper(),
        "ready": payload is not None,
        "data": payload,
    }
