"""Reactions to bookings changing inside Care. Receivers are connected at import and gated at runtime; writes made by
the plugin itself run under ``suppress_beckn_signals`` and never loop back."""

from care.emr.models.scheduling.booking import TokenBooking
from django.db import transaction
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from care_ccn_be import events
from care_ccn_be.context_vars import beckn_signals_suppressed, pop_reschedule, remember_reschedule
from care_ccn_be.flows.appointment.constants import NOTIFY_BOOKING_STATUSES
from care_ccn_be.flows.appointment.models import AppointmentLink
from care_ccn_be.flows.appointment.tasks import send_appointment_update
from care_ccn_be.flows.registry import FlowRegistry

RESCHEDULED = "rescheduled"
FULFILLED = "fulfilled"


def active() -> bool:
    return FlowRegistry.is_enabled("appointment") and not beckn_signals_suppressed()


def active_link(booking_id) -> AppointmentLink | None:
    return (
        AppointmentLink.objects.select_related("transaction").filter(token_booking_id=booking_id, active=True).first()
    )


def notify(link: AppointmentLink, action: str) -> None:
    link_id = link.pk
    transaction.on_commit(lambda: send_appointment_update.delay(link_id, action))


@receiver(pre_save, sender=TokenBooking, dispatch_uid="care_ccn_be.appointment.booking_pre_save")
def remember_previous_status(sender, instance, **kwargs):
    if instance.pk and active():
        instance._beckn_previous_status = (
            TokenBooking.objects.filter(pk=instance.pk).values_list("status", flat=True).first()
        )


@receiver(post_save, sender=TokenBooking, dispatch_uid="care_ccn_be.appointment.booking_post_save")
def booking_saved(sender, instance, created, **kwargs):
    if not active():
        return
    if created:
        old_booking_id = pop_reschedule(instance.patient_id)
        link = active_link(old_booking_id) if old_booking_id else None
        if link is not None:
            link.token_booking = instance
            link.beckn_ref = str(instance.external_id)
            link.save(update_fields=["token_booking", "beckn_ref"])
            txn = link.transaction
            txn.contract_id = str(instance.external_id)
            txn.save(update_fields=["contract_id", "modified_date"])
            notify(link, "update")
        return

    previous = getattr(instance, "_beckn_previous_status", None)
    if previous == instance.status or instance.status not in NOTIFY_BOOKING_STATUSES:
        return
    link = active_link(instance.pk)
    if link is None:
        return
    if instance.status == RESCHEDULED:
        remember_reschedule(instance.patient_id, instance.pk)
        return
    notify(link, "status")
    if instance.status == FULFILLED:
        events.booking_fulfilled.send(sender=TokenBooking, transaction=link.transaction, booking=instance)
