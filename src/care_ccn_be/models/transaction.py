from care.utils.models.base import BaseModel
from django.conf import settings
from django.db import models

from care_ccn_be.models.choices import Adapter, TxnStatus


class BecknTransaction(BaseModel):
    """One row per (transaction_id, adapter); a loopback exchange has a BAP row and a BPP row."""

    transaction_id = models.CharField(max_length=255)
    adapter = models.CharField(max_length=3, choices=Adapter.choices)
    flow = models.CharField(max_length=50)
    status = models.CharField(max_length=30, default=TxnStatus.QUEUED)
    status_message_id = models.CharField(max_length=255, blank=True)
    network_id = models.CharField(max_length=255, blank=True)
    counterparty_id = models.CharField(max_length=255, blank=True)
    counterparty_uri = models.URLField(max_length=1024, blank=True)
    contract_id = models.CharField(max_length=255, blank=True, db_index=True)
    coordination_id = models.CharField(max_length=255, blank=True, db_index=True)
    patient = models.ForeignKey("emr.Patient", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    facility = models.ForeignKey(
        "facility.Facility", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    state = models.JSONField(default=dict)
    last_error = models.JSONField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        constraints = [models.UniqueConstraint(fields=["transaction_id", "adapter"], name="beckn_txn_adapter_unique")]
        indexes = [models.Index(fields=["flow", "adapter", "status"])]

    def __str__(self):
        return f"{self.flow}/{self.adapter}/{self.transaction_id}"
