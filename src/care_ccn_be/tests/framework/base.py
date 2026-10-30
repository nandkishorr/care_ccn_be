import json
from unittest import mock

from care.utils.tests.base import CareAPITestBase
from django.test import override_settings
from rest_framework.test import APIClient

from care_ccn_be.models import BecknMessage, BecknTransaction, Side
from care_ccn_be.protocol.ack import is_nack
from care_ccn_be.protocol.client import ACK, NACK, AckResult
from care_ccn_be.tests.framework import echo_flow

BAP_ONIX = "http://onix-bap"
BPP_ONIX = "http://onix-bpp"
PLUGIN = {
    "BECKN_ENABLED_FLOWS": ["echo"],
    "BECKN_NETWORK_ID": echo_flow.NETWORK,
    "BECKN_BAP_ID": "bap.care",
    "BECKN_BAP_URI": "http://bap.care",
    "BECKN_BPP_ID": "bpp.care",
    "BECKN_BPP_URI": "http://bpp.care",
    "BECKN_BAP_CALLER_URL": BAP_ONIX,
    "BECKN_BPP_CALLER_URL": BPP_ONIX,
}


def run_inline(func, using=None, robust=False):
    func()


@override_settings(ROOT_URLCONF="care_ccn_be.tests.framework.urls", PLUGIN_CONFIGS={"care_ccn_be": PLUGIN})
class FrameworkTestBase(CareAPITestBase):
    """Celery is eager and ``on_commit`` runs inline: a whole BAP -> ONIX -> BPP -> ONIX -> BAP loop is synchronous."""

    def setUp(self):
        super().setUp()
        echo_flow.HOOKS.clear()
        patcher = mock.patch("django.db.transaction.on_commit", side_effect=run_inline)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.user = self.create_user()
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.anon = APIClient()
        self.sent: list[tuple[str, str, dict]] = []

    def loopback(self, nack_actions=()):
        """Route Care's outbound calls straight to its own receivers, as ONIX would."""
        anon = self.anon
        sent = self.sent

        def post(client, action, payload):
            sent.append((client.base_url, action, payload))
            if action in nack_actions:
                return AckResult(NACK, {"statusCode": 400, "body": {}, "error": {"code": "X"}})
            path = "bpp" if client.base_url == BAP_ONIX else "bap"
            response = anon.post(f"/{path}/receiver/{action}", payload, format="json")
            body = json.loads(response.content)
            return AckResult(NACK if is_nack(body) else ACK, {"statusCode": response.status_code, "body": body})

        patcher = mock.patch("care_ccn_be.tasks.BecknClient.post", autospec=True, side_effect=post)
        patcher.start()
        self.addCleanup(patcher.stop)

    def stub_delivery(self, result=None):
        sent = self.sent

        def post(client, action, payload):
            sent.append((client.base_url, action, payload))
            return result or AckResult(ACK, {"statusCode": 200, "body": {"message": {"ack": {"status": "ACK"}}}})

        patcher = mock.patch("care_ccn_be.tasks.BecknClient.post", autospec=True, side_effect=post)
        patcher.start()
        self.addCleanup(patcher.stop)

    def bap(self, txn_id=None, action="discover", body=None, client=None):
        url = "/bap/echo/discover/" if action == "discover" else f"/bap/echo/{txn_id}/{action}/"
        return (client or self.client).post(url, body or {}, format="json")

    def bap_txn(self, txn_id):
        return BecknTransaction.objects.get(transaction_id=txn_id, side=Side.BAP)

    def bpp_txn(self, txn_id):
        return BecknTransaction.objects.get(transaction_id=txn_id, side=Side.BPP)

    @staticmethod
    def messages(txn, **filters):
        return list(BecknMessage.objects.filter(transaction=txn, **filters).order_by("pk"))


def inbound_request(action="select", txn_id="t-1", message_id="m-1", network=echo_flow.NETWORK, message=None, **ctx):
    context = {
        "networkId": network,
        "action": action,
        "version": "2.0.0",
        "bapId": "bap.remote",
        "bapUri": "http://bap.remote",
        "transactionId": txn_id,
        "messageId": message_id,
        "timestamp": "2026-01-01T00:00:00Z",
        **ctx,
    }
    return {"context": context, "message": message if message is not None else {"contract": {"id": "c-1"}}}


def inbound_callback(action, txn_id, message_id="cb-1", message=None, **ctx):
    context = {
        "networkId": echo_flow.NETWORK,
        "action": action,
        "version": "2.0.0",
        "bapId": "bap.care",
        "bapUri": "http://bap.care",
        "bppId": "bpp.remote",
        "bppUri": "http://bpp.remote",
        "transactionId": txn_id,
        "messageId": message_id,
        "timestamp": "2026-01-01T00:00:00Z",
        **ctx,
    }
    return {"context": context, "message": message if message is not None else {"contract": {"id": "c-9"}}}
