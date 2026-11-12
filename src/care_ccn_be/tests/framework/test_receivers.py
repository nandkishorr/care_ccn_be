from django.test import override_settings

from care_ccn_be import store
from care_ccn_be.models import BecknMessage, BecknTransaction, Delivery, Direction, Processing
from care_ccn_be.protocol.ack import INVALID_PAYLOAD, UNSUPPORTED_ACTION
from care_ccn_be.tests.framework import echo_flow
from care_ccn_be.tests.framework.base import PLUGIN, FrameworkTestBase, inbound_callback, inbound_request


class BppReceiverTests(FrameworkTestBase):
    def post(self, payload, action=None, **extra):
        url = "/bpp/receiver" + (f"/{action}" if action else "")
        return self.anon.post(url, payload, format="json", **extra)

    def test_valid_request_is_acked_processed_and_answered_with_a_callback(self):
        self.stub_delivery()
        response = self.post(inbound_request("select"), "select")
        self.assertEqual(response.json(), {"message": {"ack": {"status": "ACK"}}})
        txn = self.bpp_txn("t-1")
        self.assertEqual((txn.flow, txn.coordination_id, txn.counterparty_id), ("echo", "coord-1", "bap.remote"))
        inbound, outbound = self.messages(txn)
        self.assertEqual(inbound.processing_status, Processing.PROCESSED)
        self.assertEqual((outbound.action, outbound.delivery_status), ("on_select", Delivery.ACKED))
        self.assertEqual(outbound.reply_to_id, inbound.pk)
        self.assertEqual(outbound.payload["message"]["contract"]["status"]["code"], "DRAFT")
        base_url, action, payload = self.sent[0]
        self.assertEqual((base_url, action), ("http://onix-bpp", "on_select"))
        self.assertEqual(payload["context"]["messageId"], "m-1")  # callback reuses the request message id
        txn.refresh_from_db()
        self.assertEqual(txn.status, "ON_SELECT")

    def test_action_can_come_from_the_context_instead_of_the_url(self):
        self.stub_delivery()
        self.assertEqual(self.post(inbound_request("select")).json()["message"]["ack"]["status"], "ACK")

    def test_a_retried_request_is_deduplicated(self):
        self.stub_delivery()
        first = self.post(inbound_request("select"), "select").json()
        second = self.post(inbound_request("select"), "select").json()
        self.assertEqual(first, second)
        self.assertEqual(BecknMessage.objects.filter(direction=Direction.IN, action="select").count(), 1)
        self.assertEqual(len(self.sent), 1)

    def test_invalid_payload_is_nacked_and_recorded(self):
        payload = inbound_request("select")
        del payload["context"]["transactionId"]
        body = self.post(payload, "select").json()
        self.assertEqual(body["message"]["ack"]["status"], "NACK")
        self.assertEqual(body["error"]["code"], INVALID_PAYLOAD)
        stored = BecknMessage.objects.get(action="select", direction=Direction.IN)
        self.assertIsNone(stored.transaction)
        self.assertEqual(stored.processing_status, Processing.FAILED)

    def test_malformed_json_is_nacked(self):
        response = self.anon.post("/bpp/receiver/select", "{nope", content_type="application/json")
        self.assertEqual(response.json()["error"]["code"], INVALID_PAYLOAD)

    def test_action_the_service_lacks_is_nacked(self):
        body = self.post(inbound_request("status"), "status").json()
        self.assertEqual(body["error"]["code"], UNSUPPORTED_ACTION)

    def test_callback_action_on_the_bpp_receiver_is_nacked(self):
        body = self.post(inbound_request("on_select"), "on_select").json()
        self.assertEqual(body["error"]["code"], UNSUPPORTED_ACTION)

    def test_unmatched_flow_is_nacked(self):
        body = self.post(inbound_request("select", network="other-net"), "select").json()
        self.assertEqual(body["error"]["code"], UNSUPPORTED_ACTION)
        self.assertFalse(BecknTransaction.objects.exists())

    def test_known_transaction_routes_without_matching_the_payload(self):
        self.stub_delivery()
        self.post(inbound_request("select"), "select")
        again = inbound_request("init", message_id="m-2", network="other-net", message={"contract": {"id": "c-1"}})
        self.assertEqual(self.post(again, "init").json()["message"]["ack"]["status"], "ACK")

    def test_disabled_flow_is_not_served(self):
        with override_settings(PLUGIN_CONFIGS={"care_ccn_be": {**PLUGIN, "BECKN_ENABLED_FLOWS": []}}):
            body = self.post(inbound_request("select"), "select").json()
        self.assertEqual(body["error"]["code"], UNSUPPORTED_ACTION)

    def test_domain_error_becomes_an_error_callback(self):
        self.stub_delivery()
        self.post(inbound_request("init", message={"contract": {"id": "boom"}}), "init")
        inbound, outbound = self.messages(self.bpp_txn("t-1"))
        self.assertEqual(inbound.processing_status, Processing.FAILED)
        self.assertEqual(outbound.action, "on_init")
        self.assertEqual(outbound.payload["error"]["code"], "ECHO-1")

    def test_unexpected_exception_still_produces_an_error_callback(self):
        self.stub_delivery()
        self.post(inbound_request("init", message={"contract": {"id": "crash"}}), "init")
        inbound, outbound = self.messages(self.bpp_txn("t-1"))
        self.assertEqual(inbound.processing_status, Processing.FAILED)
        self.assertEqual(outbound.payload["error"]["code"], "PROCESSING_FAILED")

    def test_discover_answers_with_the_flow_catalog(self):
        self.stub_delivery()
        payload = inbound_request("discover", message={"intent": {}})
        self.post(payload, "discover")
        _, _, sent = self.sent[0]
        self.assertEqual(sent["message"]["catalogs"][0]["id"], "echo-catalog")

    def test_unsolicited_callback_does_not_move_the_status(self):
        self.stub_delivery()
        self.post(inbound_request("select"), "select")
        txn = self.bpp_txn("t-1")
        store.record_unsolicited(txn, echo_flow.EchoFlow, "update", {"contract": {"id": "c-1"}})
        txn.refresh_from_db()
        self.assertEqual(txn.status, "ON_SELECT")
        self.assertEqual(self.sent[-1][1], "on_update")


class ReceiverSecurityTests(FrameworkTestBase):
    def with_plugin(self, **extra):
        return override_settings(PLUGIN_CONFIGS={"care_ccn_be": {**PLUGIN, **extra}})

    def test_shared_secret_is_required_when_configured(self):
        self.stub_delivery()
        with self.with_plugin(BECKN_SHARED_SECRET="s3cret"):
            denied = self.anon.post("/bpp/receiver/select", inbound_request("select"), format="json")
            wrong = self.anon.post(
                "/bpp/receiver/select", inbound_request("select"), format="json", HTTP_X_BECKN_SECRET="nope"
            )
            ok = self.anon.post(
                "/bpp/receiver/select", inbound_request("select"), format="json", HTTP_X_BECKN_SECRET="s3cret"
            )
            bap = self.anon.post("/bap/receiver/on_select", inbound_callback("on_select", "x"), format="json")
        self.assertEqual((denied.status_code, wrong.status_code, ok.status_code), (403, 403, 200))
        self.assertEqual(bap.status_code, 403)

    def test_ip_allowlist(self):
        self.stub_delivery()
        with self.with_plugin(BECKN_ALLOWED_IPS=["10.0.0.0/8"]):
            denied = self.anon.post("/bpp/receiver/select", inbound_request("select"), format="json")
            ok = self.anon.post(
                "/bpp/receiver/select", inbound_request("select"), format="json", REMOTE_ADDR="10.1.2.3"
            )
        self.assertEqual((denied.status_code, ok.status_code), (403, 200))

    def test_open_by_default(self):
        self.stub_delivery()
        self.assertEqual(
            self.anon.post("/bpp/receiver/select", inbound_request("select"), format="json").status_code, 200
        )


class BapReceiverTests(FrameworkTestBase):
    def setUp(self):
        super().setUp()
        self.stub_delivery()
        self.txn_id = self.bap().json()["transaction_id"]

    def callback(self, action, **kwargs):
        payload = inbound_callback(action, self.txn_id, **kwargs)
        return self.anon.post(f"/bap/receiver/{action}", payload, format="json")

    def test_on_discover_from_several_responders_is_merged(self):
        for bpp, cat in (("bpp.a", "A"), ("bpp.b", "B")):
            self.callback("on_discover", bppId=bpp, bppUri=f"http://{bpp}", message={"catalogs": [{"id": cat}]})
        catalogs = self.client.get(f"/bap/echo/{self.txn_id}/catalogs/").json()["catalogs"]
        self.assertEqual([c["id"] for c in catalogs], ["A", "B"])
        txn = self.bap_txn(self.txn_id)
        self.assertEqual(txn.counterparty_id, "")  # a discover callback never picks the counterparty
        self.assertEqual(txn.status, "ON_DISCOVER")

    def test_callback_sets_counterparty_contract_and_status(self):
        self.bap(self.txn_id, "select")
        self.assertEqual(self.callback("on_select").json()["message"]["ack"]["status"], "ACK")
        txn = self.bap_txn(self.txn_id)
        self.assertEqual((txn.status, txn.counterparty_id, txn.contract_id), ("ON_SELECT", "bpp.remote", "c-9"))
        poll = self.client.get(f"/bap/echo/{self.txn_id}/").json()
        self.assertEqual(poll["actions"], ["DISCOVER", "SELECT", "ON_SELECT"])
        self.assertEqual(poll["contract_id"], "c-9")

    def test_later_requests_are_routed_to_the_learned_counterparty(self):
        self.bap(self.txn_id, "select")
        self.callback("on_select")
        self.bap(self.txn_id, "init")
        payload = next(p for _, a, p in self.sent if a == "init")
        self.assertEqual(payload["context"]["bppId"], "bpp.remote")

    def test_error_callback_sets_error_status(self):
        self.bap(self.txn_id, "select")
        payload = inbound_callback("on_select", self.txn_id)
        payload["error"] = {"code": "E1", "message": "nope"}
        self.anon.post("/bap/receiver/on_select", payload, format="json")
        txn = self.bap_txn(self.txn_id)
        self.assertEqual((txn.status, txn.last_error["code"]), ("ERROR", "E1"))

    def test_stale_callback_does_not_rewind_the_status(self):
        self.bap(self.txn_id, "select")
        self.bap(self.txn_id, "init")
        self.callback("on_select", message_id="late")
        self.assertEqual(self.bap_txn(self.txn_id).status, "INIT")

    def test_duplicate_callback_is_acked_once(self):
        self.bap(self.txn_id, "select")
        self.callback("on_select")
        self.callback("on_select")
        self.assertEqual(BecknMessage.objects.filter(action="on_select", direction=Direction.IN).count(), 1)

    def test_unknown_transaction_is_nacked(self):
        payload = inbound_callback("on_select", "ghost")
        body = self.anon.post("/bap/receiver/on_select", payload, format="json").json()
        self.assertEqual(body["error"]["code"], "70004")

    def test_request_action_on_the_bap_receiver_is_nacked(self):
        payload = inbound_callback("select", self.txn_id)
        body = self.anon.post("/bap/receiver", payload, format="json").json()
        self.assertEqual(body["error"]["code"], "70001")

    def test_flow_hook_runs_after_the_callback_is_applied(self):
        self.bap(self.txn_id, "confirm")
        self.callback("on_confirm")
        self.assertEqual(echo_flow.HOOKS, [(self.txn_id, "on_confirm")])

    def test_a_failing_hook_does_not_lose_the_status_update(self):
        self.bap(self.txn_id, "cancel")
        self.callback("on_cancel")
        self.assertEqual(self.bap_txn(self.txn_id).status, "ON_CANCEL")
        (inbound,) = BecknMessage.objects.filter(action="on_cancel", direction=Direction.IN)
        self.assertEqual(inbound.processing_status, Processing.FAILED)


class LoopbackTests(FrameworkTestBase):
    def test_full_discover_select_init_confirm_loop(self):
        self.loopback()
        txn_id = self.bap().json()["transaction_id"]
        self.assertEqual(self.bap_txn(txn_id).status, "ON_DISCOVER")
        catalogs = self.client.get(f"/bap/echo/{txn_id}/catalogs/").json()["catalogs"]
        self.assertEqual(catalogs[0]["id"], "echo-catalog")
        self.bap(txn_id, "select")
        self.bap(txn_id, "init")
        self.bap(txn_id, "confirm")
        bap, bpp = self.bap_txn(txn_id), self.bpp_txn(txn_id)
        self.assertEqual((bap.status, bpp.status), ("ON_CONFIRM", "ON_CONFIRM"))
        self.assertEqual(bap.contract_id, "echo-contract-1")
        poll = self.client.get(f"/bap/echo/{txn_id}/").json()
        self.assertEqual(poll["actions"][-2:], ["CONFIRM", "ON_CONFIRM"])
        self.assertEqual(poll["contract_id"], "echo-contract-1")
        self.assertEqual(echo_flow.HOOKS, [(txn_id, "on_confirm")])
        self.assertFalse(BecknMessage.objects.exclude(delivery_status__in=["", Delivery.ACKED]).exists())
        self.assertFalse(BecknMessage.objects.filter(processing_status=Processing.PENDING).exists())

    def test_downstream_nack_marks_the_bap_transaction(self):
        self.loopback(nack_actions=("select",))
        txn_id = self.bap().json()["transaction_id"]
        self.bap(txn_id, "select")
        self.assertEqual(self.bap_txn(txn_id).status, "NACK")

    def test_bpp_domain_error_comes_back_as_an_error_status(self):
        self.loopback()
        txn_id = self.bap().json()["transaction_id"]
        self.bap(txn_id, "select")
        self.bap(txn_id, "init", {"message": {"contract": {"id": "boom"}}})
        txn = self.bap_txn(txn_id)
        self.assertEqual((txn.status, txn.last_error["code"]), ("ERROR", "ECHO-1"))
