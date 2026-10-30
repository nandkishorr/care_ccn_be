"""Reactions to Care events. Receivers are also gated at runtime so a test or deployment that disables the flow is
unaffected, and plugin-initiated writes never loop back."""

from care.emr.models.resource_request import ResourceRequest
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from care_ccn_be.context_vars import beckn_signals_suppressed
from care_ccn_be.events import booking_fulfilled
from care_ccn_be.flows.referral.tasks import complete_referral, submit_referral
from care_ccn_be.flows.registry import FlowRegistry
from care_ccn_be.settings import plugin_settings


def active() -> bool:
    return FlowRegistry.is_enabled("referral") and not beckn_signals_suppressed()


@receiver(post_save, sender=ResourceRequest, dispatch_uid="care_ccn_be.referral.resource_request_saved")
def resource_request_saved(sender, instance, created, **kwargs):
    if not created or not active():
        return
    if (instance.extensions or {}).get("beckn", {}).get("role"):
        return
    if instance.category not in plugin_settings.BECKN_REFERRAL_CATEGORIES:
        return
    if instance.status != plugin_settings.BECKN_REFERRAL_STATUS:
        return
    transaction.on_commit(lambda: submit_referral.delay(instance.pk))


@receiver(booking_fulfilled, dispatch_uid="care_ccn_be.referral.booking_fulfilled")
def booking_fulfilled_received(sender, **kwargs):
    txn = kwargs.get("transaction")
    coordination_id = getattr(txn, "coordination_id", "")
    if coordination_id and FlowRegistry.is_enabled("referral"):
        transaction.on_commit(lambda: complete_referral.delay(coordination_id))
