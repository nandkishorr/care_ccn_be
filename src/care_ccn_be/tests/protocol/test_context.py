from django.test import SimpleTestCase

from care_ccn_be.protocol.actions import ACTIONS, callback_of, is_callback, request_of
from care_ccn_be.protocol.context import (
    ContextBuilder,
    Endpoint,
    NetworkIdentity,
    clean_overrides,
    extract_routing,
)
from care_ccn_be.protocol.discover import build_discover_intent, resource_attribute_jsonpath

IDENTITY = NetworkIdentity(
    network_id="nfh.global/testnet",
    version="2.0.0",
    bap=Endpoint("care-bap", "https://care.example/bap"),
    bpp=Endpoint("care-bpp", "https://care.example/bpp"),
)


class ReferralContext(ContextBuilder):
    discover_schema_context = "https://schema.beckn.io/HealthResource/v2.1/context.jsonld"

    def default_bpp(self):
        return Endpoint("cc-bpp", "https://cc.example/bpp")


class ActionTests(SimpleTestCase):
    def test_callback_names(self):
        self.assertEqual(callback_of("init"), "on_init")
        self.assertEqual(callback_of("on_init"), "on_init")
        self.assertTrue(is_callback("on_status"))
        self.assertFalse(is_callback("status"))
        self.assertEqual(request_of("on_confirm"), "confirm")
        self.assertEqual(len(ACTIONS), 7)


class ContextTests(SimpleTestCase):
    def setUp(self):
        self.builder = ContextBuilder(IDENTITY)
        self.referral = ReferralContext(IDENTITY)

    def test_discover_is_broadcast(self):
        ctx = self.referral.request("discover", "t1", overrides={"bppId": "x", "bppUri": "y"})
        self.assertNotIn("bppId", ctx)
        self.assertNotIn("bppUri", ctx)
        self.assertEqual(ctx["schemaContext"], [ReferralContext.discover_schema_context])

    def test_non_discover_uses_default_bpp_then_routing(self):
        self.assertEqual(self.referral.request("confirm", "t1")["bppId"], "cc-bpp")
        routed = self.referral.request("confirm", "t1", routing={"bppId": "learned", "bppUri": "https://l"})
        self.assertEqual((routed["bppId"], routed["bppUri"]), ("learned", "https://l"))

    def test_no_default_bpp_omits_fields(self):
        ctx = self.builder.request("select", "t1")
        self.assertNotIn("bppId", ctx)

    def test_protected_keys_cannot_be_overridden(self):
        ctx = self.builder.request(
            "init", "t1", overrides={"transactionId": "evil", "bapId": "evil", "action": "evil", "ttl": "PT30S"}
        )
        self.assertEqual((ctx["transactionId"], ctx["bapId"], ctx["action"]), ("t1", "care-bap", "init"))
        self.assertEqual(ctx["ttl"], "PT30S")

    def test_clean_overrides_drops_none(self):
        self.assertEqual(clean_overrides("init", {"networkId": None, "ttl": "PT1S"}), {"ttl": "PT1S"})

    def test_request_has_fresh_ids_and_identity(self):
        a, b = self.builder.request("init", "t1"), self.builder.request("init", "t1")
        self.assertNotEqual(a["messageId"], b["messageId"])
        self.assertEqual(a["networkId"], "nfh.global/testnet")

    def test_callback_reuses_message_id_and_routing(self):
        inbound = self.builder.request("init", "t1", routing={"bppId": "b", "bppUri": "u"})
        cb = self.builder.callback(inbound)
        self.assertEqual(cb["action"], "on_init")
        self.assertEqual(cb["messageId"], inbound["messageId"])
        self.assertEqual((cb["bapId"], cb["bppId"], cb["transactionId"]), ("care-bap", "b", "t1"))

    def test_callback_can_mint_new_message_id(self):
        builder = ContextBuilder(IDENTITY, reuse_message_id=False)
        inbound = builder.request("init", "t1")
        self.assertNotEqual(builder.callback(inbound)["messageId"], inbound["messageId"])

    def test_unsolicited_uses_stored_bap_routing_and_own_bpp(self):
        ctx = self.builder.unsolicited("update", "t9", {"bapId": "origin", "bapUri": "https://o"})
        self.assertEqual(ctx["action"], "on_update")
        self.assertEqual((ctx["bapId"], ctx["bppId"], ctx["transactionId"]), ("origin", "care-bpp", "t9"))

    def test_publish_has_no_bap(self):
        ctx = self.builder.publish()
        self.assertEqual(ctx["action"], "catalog/publish")
        self.assertNotIn("bapId", ctx)
        self.assertEqual(ctx["bppId"], "care-bpp")

    def test_extract_routing(self):
        self.assertEqual(
            extract_routing({"bapId": "a", "bppUri": "u", "other": 1}),
            {"bapId": "a", "bapUri": None, "bppId": None, "bppUri": "u"},
        )
        self.assertEqual(set(extract_routing(None)), {"bapId", "bapUri", "bppId", "bppUri"})


class DiscoverTests(SimpleTestCase):
    def test_jsonpath_and_combined(self):
        out = resource_attribute_jsonpath({"healthServiceType": "LAB_TEST", "acceptanceMode": "MANUAL_REVIEW", "x": ""})
        self.assertEqual(out["type"], "jsonpath")
        self.assertEqual(
            out["expression"],
            '$.catalogs[*].resources[*] ? (@.resourceAttributes.healthServiceType == "LAB_TEST" '
            '&& @.resourceAttributes.acceptanceMode == "MANUAL_REVIEW")',
        )

    def test_intent_precedence(self):
        keys = ("healthServiceType", "acceptanceMode")
        self.assertEqual(build_discover_intent({"textSearch": "heart"}, keys), {"textSearch": "heart"})
        self.assertEqual(
            build_discover_intent({"filters": {"a": 1}, "healthServiceType": "X"}, keys), {"filters": {"a": 1}}
        )
        self.assertIn("filters", build_discover_intent({"healthServiceType": "X"}, keys))
        self.assertEqual(build_discover_intent(None, keys), {})
