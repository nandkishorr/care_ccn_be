import logging

from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _

PLUGIN_NAME = "care_ccn_be"

logger = logging.getLogger(__name__)


class CareCcnBeConfig(AppConfig):
    name = PLUGIN_NAME
    verbose_name = _("Care CCN (Beckn)")
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from celery.signals import task_prerun
        from django.core.signals import request_started

        from care_ccn_be import checks  # noqa: F401
        from care_ccn_be.context_vars import reset_reschedules
        from care_ccn_be.flows.registry import FlowRegistry
        from care_ccn_be.settings import plugin_settings

        request_started.connect(reset_reschedules, dispatch_uid="care_ccn_be.reset_reschedules.request")
        task_prerun.connect(reset_reschedules, dispatch_uid="care_ccn_be.reset_reschedules.task")
        FlowRegistry.autodiscover()

        if not plugin_settings.BECKN_ALLOWED_IPS and not plugin_settings.BECKN_SHARED_SECRET:
            logger.warning(
                "care_ccn_be: neither BECKN_ALLOWED_IPS nor BECKN_SHARED_SECRET is set; "
                "the Beckn receivers accept requests from any client."
            )
