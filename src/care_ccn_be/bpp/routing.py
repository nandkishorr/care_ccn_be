from care_ccn_be.flows.registry import FlowRegistry
from care_ccn_be.models import BecknTransaction, Side


def resolve_flow(request):
    """Pick the flow an inbound request belongs to: known transaction, then known contract, then payload match."""
    context = request.context
    txn = BecknTransaction.objects.filter(transaction_id=context.transaction_id, side=Side.BPP).first()
    if txn is None:
        contract = getattr(request.message, "contract", None)
        contract_id = getattr(contract, "id", None)
        if contract_id:
            txn = BecknTransaction.objects.filter(contract_id=contract_id, side=Side.BPP).first()
    if txn is not None:
        return FlowRegistry.get(txn.flow) if FlowRegistry.is_enabled(txn.flow) else None
    return FlowRegistry.match(request)
