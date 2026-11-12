from django.db import models

from care_ccn_be.models.link import BecknLink


class AppointmentLink(BecknLink):
    """Ties a Care ``TokenBooking`` to a BPP transaction; a reschedule moves the link to the replacement booking."""

    token_booking = models.ForeignKey("emr.TokenBooking", on_delete=models.CASCADE, related_name="+")

    class Meta:
        app_label = "care_ccn_be"
        constraints = [
            models.UniqueConstraint(
                fields=["transaction"], condition=models.Q(active=True), name="appointment_link_one_active"
            )
        ]
