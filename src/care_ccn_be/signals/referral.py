"""Reactions to Care events. Receivers are also gated at runtime so a test or deployment that disables the flow is
unaffected, and plugin-initiated writes never loop back."""

import logging
from uuid import UUID

from care.emr.models.file_upload import FileUpload
from care.emr.models.referral import Referral, ReferralQuestionnaireResponse, ReferralResource
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from care_ccn_be.context_vars import beckn_signals_suppressed, suppress_beckn_signals
from care_ccn_be.events import booking_fulfilled
from care_ccn_be.flows.lookup import is_flow_enabled
from care_ccn_be.models.referral import ReferralLink
from care_ccn_be.services import artifacts, referral_sync
from care_ccn_be.services import availability as availabilities
from care_ccn_be.settings import plugin_settings
from care_ccn_be.tasks.referral import complete_referral, submit_referral

logger = logging.getLogger(__name__)


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


def sync_if_linked(referral_id) -> None:
    if referral_id and ReferralLink.objects.filter(referral_id=referral_id, active=True).exists():
        referral_sync.schedule(referral_id)


@receiver(post_save, sender=Referral, dispatch_uid="care_ccn_be.referral.referral_changed")
def referral_changed(sender, instance, created, **kwargs):
    """A status change on the receiving side is reported back; the sync works out whether anything changed."""
    if not created and active():
        sync_if_linked(instance.pk)


@receiver(post_save, sender=ReferralResource, dispatch_uid="care_ccn_be.referral.resource_shared")
def resource_shared(sender, instance, created, **kwargs):
    if created and active() and instance.resource_type == artifacts.FILE_RESOURCE:
        sync_if_linked(instance.referral_id)


@receiver(post_save, sender=FileUpload, dispatch_uid="care_ccn_be.referral.file_uploaded")
def file_uploaded(sender, instance, **kwargs):
    if (
        not active()
        or not instance.upload_completed
        or instance.deleted
        or instance.is_archived
        or artifacts.CCN_REF in (instance.meta or {})
    ):
        return
    with transaction.atomic():
        if instance.file_type == artifacts.FILE_TYPE:
            try:
                referral_external_id = UUID(str(instance.associating_id))
            except ValueError:
                logger.warning("referral file %s has an invalid associating_id", instance.external_id)
                return
            link = (
                ReferralLink.objects.filter(
                    active=True, referral__external_id=referral_external_id, referral__deleted=False
                )
                .select_related("referral")
                .first()
            )
            if link is not None:
                with suppress_beckn_signals():
                    ReferralResource.objects.get_or_create(
                        referral=link.referral,
                        resource_type=artifacts.FILE_RESOURCE,
                        resource_id=instance.external_id,
                        deleted=False,
                        defaults={
                            "attachment_type": artifacts.DEFAULT_CATEGORY,
                            "created_by": instance.created_by,
                            "updated_by": instance.updated_by,
                        },
                    )
        referral_ids = (
            ReferralResource.objects.filter(
                resource_type=artifacts.FILE_RESOURCE, resource_id=instance.external_id, deleted=False
            )
            .values_list("referral_id", flat=True)
            .distinct()
        )
        for referral_id in referral_ids:
            sync_if_linked(referral_id)


@receiver(post_save, sender=ReferralQuestionnaireResponse, dispatch_uid="care_ccn_be.referral.form_answered")
def form_answered(sender, instance, created, **kwargs):
    if created and active():
        sync_if_linked(instance.referral_id)
