from django.db import models

from care_ccn_be.models.choices import Delivery, Direction, Processing
from care_ccn_be.models.transaction import BecknTransaction


class BecknMessage(models.Model):
    """Every request and callback, both directions: the audit log and the outbox in one table."""

    transaction = models.ForeignKey(BecknTransaction, null=True, on_delete=models.CASCADE, related_name="messages")
    message_id = models.CharField(max_length=255)
    action = models.CharField(max_length=30)
    direction = models.CharField(max_length=3, choices=Direction.choices)
    sender_id = models.CharField(max_length=255, default="", blank=True)
    payload = models.JSONField()
    reply_to = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="replies")
    ack = models.JSONField(null=True, blank=True)
    delivery_status = models.CharField(max_length=10, blank=True, choices=Delivery.choices)
    processing_status = models.CharField(max_length=10, blank=True, choices=Processing.choices)
    attempts = models.PositiveSmallIntegerField(default=0)
    last_error = models.TextField(blank=True)
    created_date = models.DateTimeField(auto_now_add=True, db_index=True)
    modified_date = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["message_id", "action", "direction", "sender_id"], name="beckn_msg_dedupe")
        ]
        indexes = [
            models.Index(fields=["direction", "delivery_status", "modified_date"]),
            models.Index(fields=["direction", "processing_status", "modified_date"]),
        ]

    def __str__(self):
        return f"{self.direction} {self.action} {self.message_id}"
