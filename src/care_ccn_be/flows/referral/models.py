from django.db import models

from care_ccn_be.models.link import BecknLink


class ReferralRole(models.TextChoices):
    ORIGIN = "origin"
    ASSIGNED = "assigned"


class ReferralLink(BecknLink):
    """Ties a Care ``Referral`` to the transaction that created or tracks it.

    A referral is identified by (the transaction's coordination id, role); the same referral is reused across
    the BAP and BPP rows of a loopback exchange. ``details`` keeps what Care's Referral has no column for
    (urgency tier, origin facility, referrer contact).
    """

    referral = models.ForeignKey("emr.Referral", on_delete=models.CASCADE, related_name="+")
    role = models.CharField(max_length=10, choices=ReferralRole.choices)
    details = models.JSONField(default=dict, blank=True)
    legacy_resource_request = models.ForeignKey(
        "emr.ResourceRequest", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        app_label = "care_ccn_be"
        constraints = [
            models.UniqueConstraint(
                fields=["transaction", "role"], condition=models.Q(active=True), name="beckn_referral_link_unique"
            )
        ]
        indexes = [models.Index(fields=["role", "active"])]

    def __str__(self):
        return f"{self.role}:{self.referral_id}"
