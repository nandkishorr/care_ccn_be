from celery import shared_task

from care_ccn_be import store
from care_ccn_be.flows.appointment import AppointmentFlow
from care_ccn_be.models.appointment import AppointmentLink
from care_ccn_be.resources.builders.appointment import echo_with_booking
from care_ccn_be.resources.catalog.appointment import health_service_type
from care_ccn_be.services import scheduling


@shared_task(name="care_ccn_be.appointment.notify")
def send_appointment_update(link_id: int, action: str = "status") -> str:
    """Tell the booking's BAP about a change made inside Care: an unsolicited ``on_status`` / ``on_update``."""
    flow = AppointmentFlow
    link = AppointmentLink.objects.select_related("transaction").filter(pk=link_id, active=True).first()
    if link is None:
        return "skipped"
    booking = scheduling.find_booking(link.token_booking.external_id)
    resource = booking.token_slot.resource
    service_type = health_service_type(resource, scheduling.resource_schedules(resource))
    message = echo_with_booking({"contract": {}}, booking, service_type)
    store.record_unsolicited(link.transaction, flow, action, message)
    return "sent"
