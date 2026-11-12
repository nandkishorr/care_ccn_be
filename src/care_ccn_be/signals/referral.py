"""Reactions to Care events. Receivers are also gated at runtime so a test or deployment that disables the flow is
unaffected, and plugin-initiated writes never loop back."""

from care.emr.models.referral import Referral
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from care_ccn_be.context_vars import beckn_signals_suppressed
from care_ccn_be.events import booking_fulfilled
from care_ccn_be.flows.lookup import is_flow_enabled
from care_ccn_be.services import availability as availabilities
from care_ccn_be.settings import plugin_settings
from care_ccn_be.tasks.referral import complete_referral, submit_referral


def active() -> bool:
    return is_flow_enabled("referral") and not beckn_signals_suppressed()


@receiver(post_save, sender=Referral, dispatch_uid="care_ccn_be.referral.referral_saved")
def referral_saved(sender, instance, created, **kwargs):
    """A new pending referral on a network availability (a Beckn target) is sent to that target."""
    if not created or not active():
        return
    availability = instance.availability
    if not availabilities.is_network(availability):
        return
    if availability.referral_type not in plugin_settings.BECKN_REFERRAL_TYPES:
        return
    if instance.status != plugin_settings.BECKN_REFERRAL_STATUS:
        return
    transaction.on_commit(lambda: submit_referral.delay(instance.pk))


@receiver(booking_fulfilled, dispatch_uid="care_ccn_be.referral.booking_fulfilled")
def booking_fulfilled_received(sender, **kwargs):
    txn = kwargs.get("transaction")
    coordination_id = getattr(txn, "coordination_id", "")
    if coordination_id and is_flow_enabled("referral"):
        transaction.on_commit(lambda: complete_referral.delay(coordination_id))
