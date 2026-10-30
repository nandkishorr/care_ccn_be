from django.db import models


class BecknLink(models.Model):
    """The only way a Care row is tied to a transaction; each flow adds its own subclass."""

    transaction = models.ForeignKey(
        "care_ccn_be.BecknTransaction", on_delete=models.CASCADE, related_name="%(class)s_set"
    )
    beckn_ref = models.CharField(max_length=255, blank=True)
    active = models.BooleanField(default=True)
    created_date = models.DateTimeField(auto_now_add=True)

    class Meta:
        abstract = True
