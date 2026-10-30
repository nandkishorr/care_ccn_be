import json
from unittest import mock

import requests
from django.test import SimpleTestCase

from care_ccn_be.protocol.client import ACK, ERROR, NACK, SKIPPED, BecknClient

PAYLOAD = {"context": {"transactionId": "t1"}, "message": {}}


def _response(status=200, body=None, text=None):
    response = mock.Mock(status_code=status, text=text or json.dumps(body))
    if body is None and text is not None:
        response.json.side_effect = ValueError
    else:
        response.json.return_value = body
    return response


class ClientTests(SimpleTestCase):
    def test_skipped_without_url(self):
        self.assertEqual(BecknClient("").post("init", PAYLOAD).result, SKIPPED)

    @mock.patch("care_ccn_be.protocol.client.requests.post")
    def test_ack_posts_to_action_url(self, post):
        post.return_value = _response(body={"message": {"ack": {"status": "ACK"}}})
        result = BecknClient("http://onix/bap/caller/", timeout=3).post("confirm", PAYLOAD)
        self.assertTrue(result.ok)
        self.assertEqual(result.result, ACK)
        post.assert_called_once_with("http://onix/bap/caller/confirm", json=PAYLOAD, timeout=3)

    @mock.patch("care_ccn_be.protocol.client.requests.post")
    def test_nack_body_with_200(self, post):
        post.return_value = _response(body={"message": {"ack": {"status": "NACK"}}, "error": {"code": "70002"}})
        result = BecknClient("http://onix").post("confirm", PAYLOAD)
        self.assertEqual(result.result, NACK)
        self.assertEqual(result.detail["error"], {"code": "70002"})
        self.assertFalse(result.retryable)

    @mock.patch("care_ccn_be.protocol.client.requests.post")
    def test_5xx_is_retryable_nack(self, post):
        post.return_value = _response(status=502, text="bad gateway")
        result = BecknClient("http://onix").post("confirm", PAYLOAD)
        self.assertEqual(result.result, NACK)
        self.assertTrue(result.retryable)
        self.assertEqual(result.detail["body"], "bad gateway")

    @mock.patch("care_ccn_be.protocol.client.requests.post")
    def test_4xx_is_not_retryable(self, post):
        post.return_value = _response(status=400, body={"error": {"code": "x"}})
        self.assertFalse(BecknClient("http://onix").post("confirm", PAYLOAD).retryable)

    @mock.patch("care_ccn_be.protocol.client.requests.post", side_effect=requests.ConnectionError("down"))
    def test_network_error_is_retryable(self, post):
        result = BecknClient("http://onix").post("confirm", PAYLOAD)
        self.assertEqual(result.result, ERROR)
        self.assertTrue(result.retryable)
