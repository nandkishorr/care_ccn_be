from care_ccn_be.models import Delivery, Direction
from care_ccn_be.protocol.client import ERROR, AckResult
from care_ccn_be.tasks import deliver_beckn_message
from care_ccn_be.tests.framework.base import FrameworkTestBase


class BapViewSetTests(FrameworkTestBase):
    def test_discover_is_accepted_queued_and_delivered_to_the_caller(self):
        self.stub_delivery()
        response = self.bap()
        self.assertEqual(response.status_code, 202)
        body = response.json()
        self.assertEqual(body["status"], "QUEUED")
        self.assertEqual(body["action"], "discover")
        txn = self.bap_txn(body["transactionId"])
        (msg,) = self.messages(txn)
        self.assertEqual(msg.delivery_status, Delivery.ACKED)
        self.assertEqual(msg.direction, Direction.OUT)
        base_url, action, payload = self.sent[0]
        self.assertEqual((base_url, action), ("http://onix-bap", "discover"))
        self.assertNotIn("bppId", payload["context"])  # discover is a broadcast
        self.assertEqual(payload["context"]["transactionId"], txn.transaction_id)
        txn.refresh_from_db()
        self.assertEqual(txn.status, "DISCOVER")

    def test_status_stays_queued_until_the_caller_acks(self):
        from unittest import mock

        with mock.patch("care_ccn_be.store.enqueue_delivery"):
            body = self.bap().json()
        self.assertEqual(self.bap_txn(body["transactionId"]).status, "QUEUED")

    def test_context_overrides_are_remembered_for_later_actions(self):
        self.stub_delivery()
        txn_id = self.bap().json()["transactionId"]
        self.bap(txn_id, "select", {"context": {"bppId": "bpp.x", "bppUri": "http://bpp.x", "messageId": "evil"}})
        self.bap(txn_id, "init")
        selects = [p for _, a, p in self.sent if a == "select"]
        inits = [p for _, a, p in self.sent if a == "init"]
        self.assertEqual(selects[0]["context"]["bppId"], "bpp.x")
        self.assertNotEqual(selects[0]["context"]["messageId"], "evil")
        self.assertEqual(inits[0]["context"]["bppUri"], "http://bpp.x")

    def test_passthrough_message_replaces_the_builder(self):
        self.stub_delivery()
        txn_id = self.bap().json()["transactionId"]
        self.bap(txn_id, "select", {"message": {"contract": {"id": "custom"}}})
        payload = next(p for _, a, p in self.sent if a == "select")
        self.assertEqual(payload["message"], {"contract": {"id": "custom"}})

    def test_unknown_spec_field_is_a_400(self):
        response = self.bap(body={"bogus": 1})
        self.assertEqual(response.status_code, 400)
        self.assertIn("errors", response.json())

    def test_unknown_transaction_is_a_404(self):
        response = self.bap("nope", "select")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.client.get("/bap/echo/nope/").status_code, 404)

    def test_anonymous_is_rejected(self):
        self.assertIn(self.bap(client=self.anon).status_code, (401, 403))

    def test_missing_caller_url_marks_the_transaction_not_configured(self):
        from django.test import override_settings

        from care_ccn_be.tests.framework.base import PLUGIN

        with override_settings(PLUGIN_CONFIGS={"care_ccn_be": {**PLUGIN, "BECKN_BAP_CALLER_URL": ""}}):
            txn_id = self.bap().json()["transactionId"]
        txn = self.bap_txn(txn_id)
        self.assertEqual(txn.status, "ERROR")
        self.assertEqual(txn.last_error["code"], "NOT_CONFIGURED")
        self.assertEqual(self.messages(txn)[0].delivery_status, Delivery.SKIPPED)

    def test_onix_nack_is_shown_in_the_poll(self):
        self.stub_delivery(
            AckResult("nack", {"statusCode": 200, "body": {}, "error": {"code": "70002", "message": "bad"}})
        )
        txn_id = self.bap().json()["transactionId"]
        poll = self.client.get(f"/bap/echo/{txn_id}/").json()
        self.assertEqual(poll["status"], "NACK")
        self.assertEqual(poll["error"]["code"], "70002")

    def test_retryable_delivery_raises_and_final_failure_marks_the_transaction(self):
        from unittest import mock

        with mock.patch("care_ccn_be.store.enqueue_delivery"):
            txn_id = self.bap().json()["transactionId"]
        txn = self.bap_txn(txn_id)
        (msg,) = self.messages(txn)
        self.stub_delivery(AckResult(ERROR, {"reason": "timeout"}))
        from care_ccn_be.protocol.client import TransportError

        with self.assertRaises(TransportError):
            deliver_beckn_message.run(msg.pk)
        msg.refresh_from_db()
        self.assertEqual((msg.delivery_status, msg.attempts), (Delivery.QUEUED, 1))
        deliver_beckn_message.on_failure(TransportError("timeout"), "id", [msg.pk], {}, None)
        msg.refresh_from_db()
        txn.refresh_from_db()
        self.assertEqual(msg.delivery_status, Delivery.FAILED)
        self.assertEqual((txn.status, txn.last_error["code"]), ("ERROR", "DELIVERY_FAILED"))

    def test_delivery_is_idempotent(self):
        self.stub_delivery()
        txn_id = self.bap().json()["transactionId"]
        (msg,) = self.messages(self.bap_txn(txn_id))
        self.assertEqual(deliver_beckn_message.run(msg.pk), "noop")
        self.assertEqual(len(self.sent), 1)

    def test_poll_shape(self):
        self.stub_delivery()
        txn_id = self.bap().json()["transactionId"]
        poll = self.client.get(f"/bap/echo/{txn_id}/").json()
        for key in (
            "transactionId",
            "flow",
            "serviceType",
            "status",
            "routing",
            "context",
            "actions",
            "contractId",
            "error",
        ):
            self.assertIn(key, poll)
        self.assertEqual(poll["actions"], ["DISCOVER"])
        self.assertEqual(poll["routing"]["bapId"], "bap.care")

    def test_action_view_for_a_request_returns_what_was_sent(self):
        self.stub_delivery()
        txn_id = self.bap().json()["transactionId"]
        view = self.client.get(f"/bap/echo/{txn_id}/?action=discover").json()
        self.assertTrue(view["ready"])
        self.assertEqual(view["data"]["context"]["action"], "discover")
        self.assertFalse(self.client.get(f"/bap/echo/{txn_id}/?action=on_select").json()["ready"])
