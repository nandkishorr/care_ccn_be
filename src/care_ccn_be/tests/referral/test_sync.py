from unittest import mock

from care.emr.models.file_upload import FileUpload
from care.emr.models.questionnaire import Questionnaire
from care.emr.models.referral import Referral, ReferralQuestionnaireResponse, ReferralResource
from django.test import override_settings
from model_bakery import baker

from care_ccn_be.context_vars import suppress_beckn_signals
from care_ccn_be.models import BecknMessage, Direction
from care_ccn_be.models.referral import ReferralLink
from care_ccn_be.resources.beckn import Document
from care_ccn_be.resources.builders.referral import sync_message
from care_ccn_be.resources.referral.schema import ReferralAttachment
from care_ccn_be.services import artifacts
from care_ccn_be.services import referral as lifecycle
from care_ccn_be.tasks.referral import import_referral_file, sync_referral
from care_ccn_be.tests.referral.base import REFERRAL_PLUGIN, ReferralTestBase

FILES_HOST = "files.remote"
SYNC_PLUGIN = {**REFERRAL_PLUGIN, "BECKN_ARTIFACT_MAX_BYTES": 1024}
SNAPSHOT = {
    "id": "q-remote",
    "title": "Cardiology findings",
    "version": "1.0",
    "questions": [{"id": "q1", "text": "Findings", "type": "string"}],
}
RESULTS = {"results": [{"question_id": "q1", "values": [{"value": "Normal ECG"}]}]}


def http_response(content=b"%PDF-1.4", status=200, headers=None):
    response = mock.MagicMock()
    response.status_code = status
    response.headers = headers or {}
    response.iter_content.return_value = [content]
    response.__enter__.return_value = response
    return response


def remote_document(ref="file-remote-1", url=f"https://{FILES_HOST}/ecg", mime_type="application/pdf"):
    return {"id": ref, "label": ref, "name": "ECG", "url": url, "mimeType": mime_type}


def remote_sync(status=None, category="source", ref="file-remote-1", responses=True, **document):
    referral = {"attachments": [{"ref": ref, "category": category, "name": "ECG", "extension": ".pdf"}]}
    if responses:
        referral["responses"] = [
            {
                "ref": "response-remote-1",
                "questionnaireType": "response" if category == "response" else "request",
                "status": "completed",
                "revision": 1,
                "questionnaire": SNAPSHOT,
                "questionnaireResponse": RESULTS,
                "author": "Dr Rao",
            }
        ]
    if status:
        referral["status"] = status
    return {
        "contract": {
            "descriptor": {"docs": [remote_document(ref, **document)]},
            "contractAttributes": {"@type": "hrf:HealthReferral", "coordinationId": "coord-1", "referral": referral},
        }
    }


@override_settings(PLUGIN_CONFIGS={"care_ccn_be": SYNC_PLUGIN})
class SyncTestBase(ReferralTestBase):
    def setUp(self):
        super().setUp()
        self.signed = mock.patch.object(
            FileUpload.files_manager, "read_signed_url", side_effect=lambda f, **_: f"https://{FILES_HOST}/{f.pk}"
        )
        self.put = mock.patch.object(FileUpload.files_manager, "put_object")
        self.get = mock.patch("care_ccn_be.services.artifacts.requests.get", return_value=http_response())
        self.put_object = self.put.start()
        self.signed.start()
        self.http_get = self.get.start()
        for patcher in (self.signed, self.put, self.get):
            self.addCleanup(patcher.stop)

    def upload_file(self, referral, name="ecg", upload_completed=False, **kwargs):
        return baker.make(
            FileUpload,
            name=name,
            internal_name=f"{name}.pdf",
            file_type="referral",
            file_category="unspecified",
            associating_id=str(referral.external_id),
            upload_completed=upload_completed,
            meta={"mime_type": "application/pdf"},
            **kwargs,
        )

    def share_file(self, referral, category="source", name="ecg"):
        file = self.upload_file(referral, name=name)
        ReferralResource.objects.create(
            referral=referral, resource_type="file_upload", resource_id=file.external_id, attachment_type=category
        )
        file.upload_completed = True
        file.save(update_fields=["upload_completed"])
        return file

    def answer(self, referral, questionnaire_type="request"):
        questionnaire = baker.make(Questionnaire, title="Referral form", subject_type="referral", status="active")
        return ReferralQuestionnaireResponse.objects.create(
            referral=referral,
            questionnaire=questionnaire,
            questionnaire_response=RESULTS,
            questionnaire_snapshot={**SNAPSHOT, "id": str(questionnaire.external_id)},
            questionnaire_type=questionnaire_type,
            created_by=self.user,
        )

    def sent_actions(self):
        return [action for _, action, _ in self.sent]

    def last(self, action):
        return next(payload for _, a, payload in reversed(self.sent) if a == action)


class SyncMessageTests(SyncTestBase):
    def test_files_answers_and_status_travel_in_one_contract(self):
        document = Document(id="file-1", label="file-1", name="ECG", url="https://x/1", mime_type="application/pdf")
        attachments = [
            ReferralAttachment(ref="file-1", category="source", name="ECG"),
            ReferralAttachment(ref="file-2", category="metadata"),
        ]
        message = sync_message("coord-1", [document], attachments, [], status="completed", contract_id="c-1")
        contract = message["contract"]
        self.assertEqual(contract["id"], "c-1")
        self.assertEqual(contract["status"]["code"], "COMPLETED")
        self.assertEqual(contract["descriptor"]["docs"][0]["mimeType"], "application/pdf")
        attributes = contract["contractAttributes"]
        self.assertEqual(attributes["@type"], "hrf:HealthReferral")
        self.assertEqual(attributes["lifecycleState"], "FULFILLED")
        self.assertEqual(attributes["referralNote"]["priorResultRefs"], ["file-1"])
        self.assertEqual([a["category"] for a in attributes["referral"]["attachments"]], ["source", "metadata"])
        self.assertNotIn("responses", attributes["referral"])

    def test_nothing_new_has_no_status(self):
        contract = sync_message("coord-1", [], [], [])["contract"]
        self.assertNotIn("status", contract)
        self.assertNotIn("descriptor", contract)


class OriginSyncTests(SyncTestBase):
    """The referring side: what is added to a referral Care sent is reported with ``update``."""

    def setUp(self):
        super().setUp()
        self.stub_delivery()
        self.referral = baker.make(
            Referral,
            availability=self.network_availability(),
            patient=self.create_patient(name="Asha"),
            status="pending",
            title="Heart",
            assignees=[],
            created_by=self.user,
        )
        self.txn_id = str(self.referral.external_id)

    def confirm(self):
        self.callback("on_confirm", self.txn_id, {"contract": {"id": "c-1", "status": {"code": "ACTIVE"}}})

    def test_completed_upload_is_automatically_shared_and_sent(self):
        self.confirm()
        file = self.upload_file(self.referral)
        self.assertFalse(ReferralResource.objects.filter(referral=self.referral).exists())
        self.assertEqual(self.sent_actions(), ["confirm"])

        file.upload_completed = True
        file.save(update_fields=["upload_completed"])

        resource = ReferralResource.objects.get(referral=self.referral, resource_id=file.external_id)
        self.assertEqual(resource.attachment_type, "source")
        self.assertEqual(self.sent_actions(), ["confirm", "update"])
        attachment = self.last("update")["message"]["contract"]["contractAttributes"]["referral"]["attachments"][0]
        self.assertEqual(attachment["ref"], f"file-{file.external_id}")

        file.save()
        self.assertEqual(self.sent_actions(), ["confirm", "update"])
        self.assertEqual(ReferralResource.objects.filter(referral=self.referral).count(), 1)

    def test_upload_created_complete_is_sent(self):
        self.confirm()
        self.upload_file(self.referral, upload_completed=True)
        self.assertEqual(self.sent_actions(), ["confirm", "update"])

    def test_completed_upload_before_confirm_waits_for_confirmation(self):
        self.upload_file(self.referral, upload_completed=True)
        self.assertEqual(self.sent_actions(), ["confirm"])
        self.confirm()
        self.assertEqual(self.sent_actions(), ["confirm", "update"])

    def test_excluded_uploads_are_not_automatically_shared(self):
        self.confirm()
        for flags in ({"deleted": True}, {"is_archived": True}):
            with self.subTest(flags=flags):
                self.upload_file(self.referral, upload_completed=True, **flags)
        mirrored = self.upload_file(self.referral)
        mirrored.meta["ccn_ref"] = "file-remote"
        mirrored.upload_completed = True
        mirrored.save()
        with suppress_beckn_signals():
            self.upload_file(self.referral, upload_completed=True)
        with override_settings(PLUGIN_CONFIGS={"care_ccn_be": {**SYNC_PLUGIN, "BECKN_ENABLED_FLOWS": []}}):
            self.upload_file(self.referral, upload_completed=True)
        self.assertFalse(ReferralResource.objects.filter(referral=self.referral).exists())
        self.assertEqual(self.sent_actions(), ["confirm"])

    def test_unlinked_referral_upload_stays_local(self):
        local = self.create_referral()
        self.upload_file(local, upload_completed=True)
        self.assertFalse(ReferralResource.objects.filter(referral=local).exists())
        self.assertEqual(self.sent_actions(), ["confirm"])

    def test_files_shared_after_confirm_are_sent_as_an_update(self):
        self.confirm()
        self.assertEqual(self.sent_actions(), ["confirm"])
        file = self.share_file(self.referral)
        self.assertEqual(self.sent_actions(), ["confirm", "update"])
        payload = self.last("update")
        self.assertEqual(payload["context"]["bppId"], "bpp.remote")
        self.assertEqual(payload["context"]["transactionId"], self.txn_id)
        contract = payload["message"]["contract"]
        self.assertEqual(contract["id"], "c-1")
        ref = f"file-{file.external_id}"
        url = f"https://{FILES_HOST}/{file.pk}"
        self.assertEqual(
            contract["descriptor"]["docs"],
            [{"id": ref, "label": ref, "name": "ecg", "url": url, "mimeType": "application/pdf"}],
        )
        attributes = contract["contractAttributes"]
        self.assertEqual(
            attributes["referral"]["attachments"],
            [{"ref": ref, "category": "source", "name": "ecg", "extension": ".pdf"}],
        )
        self.assertEqual(attributes["referralNote"]["priorResultRefs"], [ref])
        self.assertNotIn("status", contract)

    def test_remote_referral_id_and_latest_callback_routing_are_used_for_updates(self):
        txn = self.bap_txn(self.txn_id)
        txn.state = {"context_overrides": {"bppId": "old.bpp", "bppUri": "http://old.bpp"}}
        txn.save(update_fields=["state"])
        message = {
            "contract": {
                "id": "contract-1",
                "status": {"code": "ACTIVE"},
                "contractAttributes": {"referral": {"id": "remote-referral-1"}},
            }
        }
        self.callback("on_confirm", self.txn_id, message, bppId="new.bpp", bppUri="http://new.bpp")
        self.assertEqual(
            ReferralLink.objects.get(referral=self.referral).details["external_referral_id"], "remote-referral-1"
        )
        self.share_file(self.referral)
        payload = self.last("update")
        self.assertEqual((payload["context"]["bppId"], payload["context"]["bppUri"]), ("new.bpp", "http://new.bpp"))
        contract = payload["message"]["contract"]
        self.assertEqual(contract["id"], "contract-1")
        self.assertEqual(contract["contractAttributes"]["referral"]["id"], "remote-referral-1")

    def test_error_confirmation_does_not_unblock_sync(self):
        self.callback(
            "on_confirm",
            self.txn_id,
            {"contract": {"id": "c-1", "status": {"code": "ACTIVE"}}},
            error={"code": "X"},
        )
        self.upload_file(self.referral, upload_completed=True)
        self.assertEqual(self.sent_actions(), ["confirm"])
        self.callback(
            "on_confirm",
            self.txn_id,
            {"contract": {"id": "c-1", "status": {"code": "ACTIVE"}}},
            message_id="cb-2",
        )
        self.assertEqual(self.sent_actions(), ["confirm", "update"])

    def test_unprocessed_confirmation_does_not_unblock_sync(self):
        with mock.patch("care_ccn_be.tasks.process_bap_callback.apply_async"):
            self.confirm()
        self.upload_file(self.referral, upload_completed=True)
        self.assertEqual(self.sent_actions(), ["confirm"])

    def test_sync_still_works_after_the_confirmation_message_is_purged(self):
        self.confirm()
        BecknMessage.objects.filter(transaction=self.bap_txn(self.txn_id), action="on_confirm").delete()
        self.upload_file(self.referral, upload_completed=True)
        self.assertEqual(self.sent_actions(), ["confirm", "update"])

    def test_existing_confirmed_link_recovers_remote_id_from_callback(self):
        message = {
            "contract": {
                "id": "contract-1",
                "status": {"code": "ACTIVE"},
                "contractAttributes": {"referral": {"id": "remote-referral-1"}},
            }
        }
        self.callback("on_confirm", self.txn_id, message)
        link = ReferralLink.objects.get(referral=self.referral)
        link.details = {}
        link.save(update_fields=["details"])
        self.upload_file(self.referral, upload_completed=True)
        link.refresh_from_db()
        self.assertEqual(link.details["external_referral_id"], "remote-referral-1")
        contract = self.last("update")["message"]["contract"]
        self.assertEqual(contract["contractAttributes"]["referral"]["id"], "remote-referral-1")

    def test_each_item_is_sent_once(self):
        self.confirm()
        self.share_file(self.referral)
        response = self.answer(self.referral)
        self.assertEqual(self.sent_actions(), ["confirm", "update", "update"])
        referral = self.last("update")["message"]["contract"]["contractAttributes"]["referral"]
        self.assertNotIn("attachments", referral)
        (sent,) = referral["responses"]
        self.assertEqual(sent["ref"], f"response-{response.external_id}")
        self.assertEqual(sent["questionnaireResponse"], RESULTS)
        self.assertEqual(sent["questionnaire"]["title"], "Cardiology findings")
        self.assertEqual(sync_referral(self.referral.pk), "noop")

    def test_what_was_added_before_the_confirm_is_sent_once_it_is_confirmed(self):
        self.share_file(self.referral)
        self.answer(self.referral)
        self.assertEqual(self.sent_actions(), ["confirm"])
        self.confirm()
        self.assertEqual(self.sent_actions(), ["confirm", "update"])
        referral = self.last("update")["message"]["contract"]["contractAttributes"]["referral"]
        self.assertEqual((len(referral["attachments"]), len(referral["responses"])), (1, 1))

    def test_the_origin_status_is_not_sent(self):
        self.confirm()
        self.referral.title = "Changed"
        self.referral.save()
        self.assertEqual(self.sent_actions(), ["confirm"])

    def test_mirrored_and_unfinished_files_are_not_sent(self):
        self.confirm()
        with mock.patch("care_ccn_be.services.referral_sync.schedule"):
            mirrored = self.share_file(self.referral)
            ReferralResource.objects.filter(resource_id=mirrored.external_id).update(metadata={"ccn_ref": "file-x"})
            pending = self.share_file(self.referral, name="pending")
            FileUpload.objects.filter(pk=pending.pk).update(upload_completed=False)
        self.assertEqual(sync_referral(self.referral.pk), "noop")

    def test_on_update_mirrors_the_response_files_answers_and_status(self):
        self.confirm()
        self.callback("on_update", self.txn_id, remote_sync(status="rejected", category="response"), message_id="u-1")
        self.referral.refresh_from_db()
        self.assertEqual(self.referral.status, "rejected")
        resource = ReferralResource.objects.get(referral=self.referral)
        self.assertEqual((resource.attachment_type, resource.metadata), ("response", {"ccn_ref": "file-remote-1"}))
        response = ReferralQuestionnaireResponse.objects.get(referral=self.referral)
        self.assertEqual((response.questionnaire_type, response.questionnaire_snapshot), ("response", SNAPSHOT))
        self.assertEqual(response.meta["ccn_author"], "Dr Rao")
        self.assertEqual(self.sent_actions(), ["confirm"])

    def test_a_completed_on_update_completes_the_origin(self):
        self.confirm()
        self.callback("on_update", self.txn_id, remote_sync(status="completed"), message_id="u-1")
        self.referral.refresh_from_db()
        self.assertEqual(self.referral.status, "completed")

    def test_a_closed_referral_is_never_reopened(self):
        self.confirm()
        self.callback("on_update", self.txn_id, remote_sync(status="completed", responses=False), message_id="u-1")
        self.callback("on_update", self.txn_id, remote_sync(status="in_progress", responses=False), message_id="u-2")
        self.referral.refresh_from_db()
        self.assertEqual(self.referral.status, "completed")


class AssignedSyncTests(SyncTestBase):
    """The receiving side: ``update`` is mirrored onto the assigned referral; its own changes go back as
    unsolicited ``on_update``."""

    def setUp(self):
        super().setUp()
        self.stub_delivery()
        self.request("confirm", "coord-1", self.referral_message(assigned=self.target))
        self.assigned = lifecycle.current("coord-1", lifecycle.ASSIGNED)
        self.sent.clear()

    def update(self, message, message_id="m-2"):
        return self.request("update", "coord-1", message, message_id=message_id)

    def test_completed_upload_is_automatically_sent_back(self):
        file = self.upload_file(self.assigned, upload_completed=True)
        self.assertEqual(self.sent_actions(), ["on_update"])
        resource = ReferralResource.objects.get(referral=self.assigned)
        self.assertEqual(resource.resource_id, file.external_id)
        attachment = self.last("on_update")["message"]["contract"]["contractAttributes"]["referral"]["attachments"][0]
        self.assertEqual(attachment["ref"], f"file-{file.external_id}")

    def test_an_update_attaches_the_files_and_answers_to_the_assigned_referral(self):
        self.update(remote_sync())
        resource = ReferralResource.objects.get(referral=self.assigned)
        self.assertEqual((resource.attachment_type, resource.metadata), ("source", {"ccn_ref": "file-remote-1"}))
        file = FileUpload.objects.get(external_id=resource.resource_id)
        self.assertEqual(
            (file.name, file.file_type, file.associating_id), ("ECG", "referral", str(self.assigned.external_id))
        )
        self.assertTrue(file.upload_completed)
        self.assertTrue(file.internal_name.endswith(".pdf"))
        self.assertEqual(file.meta["mime_type"], "application/pdf")
        self.put_object.assert_called_once_with(file, b"%PDF-1.4", ContentType="application/pdf")
        self.http_get.assert_called_once()
        response = ReferralQuestionnaireResponse.objects.get(referral=self.assigned)
        self.assertEqual((response.questionnaire_type, response.questionnaire_response), ("request", RESULTS))
        self.assertEqual((response.questionnaire.status, response.questionnaire.subject_type), ("retired", "referral"))
        (callback,) = [p for _, a, p in self.sent if a == "on_update"]
        self.assertNotIn("error", callback)
        self.assertNotIn("descriptor", callback["message"]["contract"])
        self.assertEqual(callback["message"]["contract"]["contractAttributes"]["referral"]["status"], "in_progress")

    def test_a_repeated_update_creates_nothing_twice(self):
        self.update(remote_sync())
        self.update(remote_sync(), message_id="m-3")
        self.assertEqual(ReferralResource.objects.filter(referral=self.assigned).count(), 1)
        self.assertEqual(ReferralQuestionnaireResponse.objects.filter(referral=self.assigned).count(), 1)
        self.assertEqual(Questionnaire.objects.filter(status="retired").count(), 1)

    def test_mirrored_rows_are_never_sent_back(self):
        self.update(remote_sync())
        self.assertEqual(self.sent_actions(), ["on_update"])
        self.assertEqual(sync_referral(self.assigned.pk), "noop")

    def test_the_status_does_not_follow_the_referring_side(self):
        self.update(remote_sync(status="completed"))
        self.assigned.refresh_from_db()
        self.assertEqual(self.assigned.status, "in_progress")

    def test_an_update_with_an_undownloadable_document_is_rejected_whole(self):
        for document in ({"url": "ftp://other.host/ecg"}, {"mime_type": "text/html"}):
            self.sent.clear()
            self.update(remote_sync(**document), message_id=f"m-{document}")
            (callback,) = [p for _, a, p in self.sent if a == "on_update"]
            self.assertEqual(callback["error"]["code"], "70002")
            self.assertIn("file-remote-1", callback["error"]["message"])
        self.http_get.assert_not_called()
        self.assertFalse(ReferralResource.objects.filter(referral=self.assigned).exists())
        self.assertFalse(ReferralQuestionnaireResponse.objects.filter(referral=self.assigned).exists())

    def test_already_imported_documents_are_not_checked_again(self):
        self.update(remote_sync())
        self.sent.clear()
        self.update(remote_sync(url="ftp://other.host/ecg"), message_id="m-3")
        (callback,) = [p for _, a, p in self.sent if a == "on_update"]
        self.assertNotIn("error", callback)

    def test_the_response_and_its_files_go_back_with_the_status(self):
        file = self.share_file(self.assigned, category="response")
        self.answer(self.assigned, questionnaire_type="response")
        self.assigned.status = "completed"
        self.assigned.save()
        self.assertEqual(self.sent_actions(), ["on_update", "on_update", "on_update"])
        payloads = [p for _, a, p in self.sent if a == "on_update"]
        self.assertEqual(payloads[0]["context"]["bapId"], "bap.remote")
        self.assertEqual(
            payloads[0]["message"]["contract"]["contractAttributes"]["referral"]["attachments"][0]["ref"],
            f"file-{file.external_id}",
        )
        first = payloads[0]["message"]["contract"]["contractAttributes"]
        self.assertNotIn("status", first["referral"])
        self.assertNotIn("referralNote", first)
        (response,) = payloads[1]["message"]["contract"]["contractAttributes"]["referral"]["responses"]
        self.assertEqual(response["questionnaireType"], "response")
        last = payloads[2]["message"]["contract"]
        self.assertEqual(
            (last["status"]["code"], last["contractAttributes"]["lifecycleState"]), ("COMPLETED", "FULFILLED")
        )
        self.assertEqual(ReferralLink.objects.get(referral=self.assigned).details["synced"]["status"], "completed")


class DownloadTests(SyncTestBase):
    def setUp(self):
        super().setUp()
        self.stub_delivery()
        self.request("confirm", "coord-1", self.referral_message(assigned=self.target))
        self.assigned = lifecycle.current("coord-1", lifecycle.ASSIGNED)

    def run_import(self, document=None, attachment=None):
        document = document or remote_document()
        attachment = attachment or {"ref": document["label"], "category": "source", "name": "ECG"}
        return import_referral_file(self.assigned.pk, document, attachment)

    def test_only_https_urls_are_fetched(self):
        for url in (
            "http://files.remote/x",
            "file:///etc/passwd",
            "https:///x",
            "",
            "https://localhost/x",
            "https://127.0.0.1/x",
            "https://[::1]/x",
        ):
            self.assertEqual(self.run_import(remote_document(url=url)), "rejected")
        self.http_get.assert_not_called()

    def test_debug_accepts_localhost_over_http(self):
        with override_settings(DEBUG=True):
            for url in ("http://localhost:9000/x", "http://127.0.0.1/x"):
                artifacts.check_url(url)

    def test_disallowed_mime_types_are_not_fetched(self):
        self.assertEqual(self.run_import(remote_document(mime_type="text/html")), "rejected")
        self.http_get.assert_not_called()

    def test_oversized_documents_are_rejected(self):
        self.http_get.return_value = http_response(headers={"Content-Length": "4096"})
        self.assertEqual(self.run_import(), "rejected")
        self.http_get.return_value = http_response(content=b"x" * 2048)
        self.assertEqual(self.run_import(), "rejected")
        self.assertFalse(ReferralResource.objects.exists())

    def test_redirects_and_client_errors_are_rejected(self):
        for status in (302, 403):
            self.http_get.return_value = http_response(status=status)
            self.assertEqual(self.run_import(), "rejected")
        self.assertEqual(self.http_get.call_args.kwargs["allow_redirects"], False)

    def test_an_import_is_idempotent(self):
        self.assertEqual(self.run_import(), "imported")
        self.assertEqual(self.run_import(), "duplicate")

    def test_prior_results_of_a_non_care_sender_are_shared_as_source_records(self):
        message = {
            "contract": {
                "descriptor": {"docs": [remote_document("ecg-prior-001")]},
                "contractAttributes": {"referralNote": {"priorResultRefs": ["ecg-prior-001", "missing"]}},
            }
        }
        expected = (remote_document("ecg-prior-001"), {"ref": "ecg-prior-001", "category": "source"})
        self.assertEqual(artifacts.incoming(message), [expected])


class LoopbackSyncTests(SyncTestBase):
    """Both instances in one: files and answers travel origin -> assigned and the response travels back."""

    def test_request_information_and_the_response_travel_between_the_referrals(self):
        from care.emr.models import Encounter

        from care_ccn_be.tasks.referral import submit_referral

        self.loopback()
        patient = self.create_patient(name="Asha", gender="female")
        encounter = baker.make(Encounter, patient=patient, facility=self.origin)
        origin = self.create_referral(
            patient=patient, encounter=encounter, availability=self.network_availability(assigned=self.target)
        )
        self.assertEqual(submit_referral(origin.pk), "sent")
        txn_id = str(origin.external_id)
        assigned = lifecycle.current(txn_id, lifecycle.ASSIGNED)
        self.assertEqual(
            ReferralLink.objects.get(referral=origin).details["external_referral_id"], str(assigned.external_id)
        )

        shared = self.upload_file(origin)
        shared.upload_completed = True
        shared.save(update_fields=["upload_completed"])
        update = self.last("update")["message"]["contract"]
        self.assertEqual(update["contractAttributes"]["referral"]["id"], str(assigned.external_id))
        self.answer(origin)
        mirrored = ReferralResource.objects.get(referral=assigned)
        self.assertEqual(mirrored.metadata, {"ccn_ref": f"file-{shared.external_id}"})
        self.assertEqual(ReferralQuestionnaireResponse.objects.get(referral=assigned).questionnaire_type, "request")

        self.share_file(assigned, category="response", name="report")
        self.answer(assigned, questionnaire_type="response")
        assigned.status = "completed"
        assigned.save()

        origin.refresh_from_db()
        self.assertEqual(origin.status, "completed")
        returned = ReferralResource.objects.get(referral=origin, attachment_type="response")
        self.assertEqual(FileUpload.objects.get(external_id=returned.resource_id).name, "report")
        types = ReferralQuestionnaireResponse.objects.filter(referral=origin).order_by("pk")
        self.assertEqual(list(types.values_list("questionnaire_type", flat=True)), ["request", "response"])
        self.assertEqual(ReferralResource.objects.filter(referral=assigned).count(), 2)
        self.assertFalse(BecknMessage.objects.filter(direction=Direction.IN, processing_status="failed").exists())
