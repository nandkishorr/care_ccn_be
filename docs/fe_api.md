# Frontend API

Routes are relative to `/api/care_ccn_be/` and follow CARE EMR conventions: snake_case JSON, EMR error bodies and
paginated lists. Every FE route needs an authenticated user. Permissions are not implemented yet: any authenticated
user can read and act on every transaction.

## Routes

Each action is its own API, contributed by a mixin on the flow's viewset. `referral` has discover, select, init,
confirm and status; `appointment` has all seven actions.

| Method | Path | Body / query | Response |
|---|---|---|---|
| `GET` | `bap/<flow>/` | `?transaction_id=&status=&contract_id=&coordination_id=&limit=&offset=` | paginated `BecknTransactionListSpec` |
| `GET` | `bap/<flow>/{transaction_id}/` | | `<Flow>TransactionRetrieveSpec` |
| `POST` | `bap/<flow>/discover/` | `BapDiscoverSpec` | `202 BapActionResponseSpec` |
| `GET` | `bap/<flow>/{transaction_id}/catalogs/` | | `BapCatalogsSpec`, the catalogs of every `on_discover` |
| `POST` | `bap/<flow>/{transaction_id}/<action>/` | `bap_<action>_spec` of the flow | `202 BapActionResponseSpec` |
| `GET` | `bap/message/` | `?transaction_id=` (required) `&action=on_select&direction=in` | paginated `BecknMessageReadSpec` |
| `GET` | `bpp/<flow>/`, `bpp/<flow>/{transaction_id}/` | as for `bap/<flow>/` | Care-as-BPP transactions |
| `GET` | `bpp/message/` | as for `bap/message/` | |

The ONIX routes are unchanged: `POST bap/receiver[/on_<action>][/]` and `POST bpp/receiver[/<action>][/]`.

## Request bodies

Every action body:

- may include `context` overrides, remembered for later actions: `network_id`, `version`, `bpp_id`, `bpp_uri`,
  `ttl`, `schema_context`. Other keys are ignored. Send `bpp_id` / `bpp_uri` once, at `select`, from the chosen
  catalog. They are dropped on discover, which always goes to the discovery service;
- may include a raw Beckn `message` (passthrough), validated against the flow's schema. The builder is skipped and
  the flow's `perform_<action>` side effects still run. Required fields only apply without a passthrough `message`;
- is rejected with `400` if it contains unknown fields or is not a JSON object.

| Spec | Fields (`*` required without a passthrough `message`) |
|---|---|
| `BapDiscoverSpec` | `health_service_type`, `acceptance_mode`, `text_search`, `filters` |
| `ReferralSelectSpec` | `offer_id*` |
| `ReferralInitSpec`, `ReferralConfirmSpec` | `facility*` (uuid), `patient` (uuid) or `patient_name`, `encounter` (uuid), `title`, `reason`, `category`, `offer_id`, `assigned_facility_id`, `emergency`, `priority`, `questionnaire` (uuid), `questionnaire_response` |
| `AppointmentSelectSpec` | `resource_id*`, `offer_id`, `date`, `health_service_type` |
| `AppointmentInitSpec` | `slot_id*`, `patient*` (uuid) |
| `AppointmentConfirmSpec` | `slot_id*`, `patient*` (uuid), `coordination_ref` |
| `AppointmentRescheduleSpec` (`update`) | `slot_id*` |
| `AppointmentCancelSpec` | `reason` |
| `BapActionSpec` (`status`) | only `context` / `message` |

`init` is optional in both flows: discover, select, confirm works. A referral `init` only validates (nothing is
created); the referrals are created on `confirm`.

A referral `encounter` stays in Care (it is not sent to the network): the origin `Referral` is linked to it, and its
patient is used when `patient` is omitted. It must belong to `patient` and `facility`, else `400`; unknown is `404`.

### Referral availabilities and their forms

A facility receives network referrals only on an active, public `ReferralAvailability` (of the facility or one of
its organizations). `publish_beckn_catalog --flow referral` publishes one catalog per such facility
(`catalog-referral-<facility id>`), one resource and offer per availability: the resource `id` is the availability
id, the offer is `offer-<availability id>`, and `resourceAttributes.referralAvailability` carries `id`,
`referralType`, `entityType`, `organizationId`, `organizationName`. Forms are not in the catalog. The catalog is
republished when a receiving availability changes (`BECKN_REFERRAL_CATALOG_AUTO_PUBLISH`, default on).

1. `select` with the chosen catalog's `offer_id` and `context.bpp_id` / `context.bpp_uri`.
2. Once `ON_SELECT`, the transaction retrieve has `availability` (from the BPP's
   `contract.contractAttributes.referral.availability`):

   ```json
   "availability": {
     "id": "…", "name": "Referral Unit TN", "description": "…", "referral_type": "consultation",
     "facility_id": "…", "facility_name": "Medicity",
     "questionnaires": [
       {"questionnaire_type": "request", "questionnaire": {"id": "…", "slug": "…", "version": "0.1", "title": "…",
         "status": "active", "subject_type": "referral", "internal_revision": 1, "questions": […]}},
       {"questionnaire_type": "response", "questionnaire": {…}}
     ]
   }
   ```

   Render the `request` questionnaire; the `response` one is filled later by the receiving facility.
3. `init` (then `confirm`) with the answered request form, in Care's questionnaire submission shape:

   ```json
   {"facility": "…", "patient": "…", "title": "…", "reason": "…",
    "questionnaire": "<request questionnaire id>",
    "questionnaire_response": {"results": [{"question_id": "…", "values": [{"value": "…"}]}]}}
   ```

   `assigned_facility_id` and `category` default to the selected availability's. The contract carries
   `referral.availability.id` and the form as `referral.responses[]` (`questionnaireType: "request"`).
4. The BPP validates the answers against its questionnaire. A rejected `init` leaves the transaction in `ERROR` with
   `last_error` (`code`, `message`, `paths` = question ids); fix the answers and call `init` again. On `ON_INIT`,
   `confirm` with the same body: both instances then create their referral with the request form attached.
5. The receiving facility answers the `response` questionnaire on its referral in Care; it is synchronized back onto
   the origin referral (see below).

The origin referral sits on a network availability for the remote one (`external_entity.beckn.availabilityId`),
whose questionnaires are local read-only copies of the remote forms.

### Automatic referral file synchronization

Completed Care uploads with `file_type="referral"` and `associating_id` set to a network-linked referral's UUID
are automatically attached as `source` resources and synchronized after commit. Existing attachments keep their
category. No separate Beckn update call is required: the origin sends `update`, and the assigned instance sends
`on_update`. Files explicitly shared as referral resources also synchronize when their upload completes.

The other instance downloads the signed file URL into its own storage and attaches a local copy to the matching
referral. Imported files are marked to prevent echoing them back; incomplete, deleted and archived files are not sent.
Origin updates wait for confirmation. Both instances must run Celery workers. Document URLs must use `https`
and must not point at `localhost` or a loopback address; when `DEBUG` is on, `http` and `localhost` are also accepted. On the receiving side of an `update`, a new document whose URL scheme
is not allowed, or whose mime type is not accepted, rejects the whole update: `on_update` carries a `70002` error and none of its files,
forms or status are applied. Download failures (expired URL, oversized file, network errors) happen after `on_update`
is sent and are only logged. Signed URL lifetime and download size
are controlled by `BECKN_ARTIFACT_URL_TTL` and `BECKN_ARTIFACT_MAX_BYTES`.
This synchronization covers file uploads and completed referral questionnaire responses. Other shared clinical
records, such as diagnostic reports, are not currently synchronized.

## Responses

```json
202 {"transaction_id": "a9046a23-…", "message_id": "…", "action": "select", "status": "QUEUED"}
```

The request is stored and delivered to the ONIX BAP caller (`{BECKN_BAP_CALLER_URL}/<action>`) after commit.
Retrieve the transaction to follow it:

```json
{
  "id": "…", "transaction_id": "a9046a23-…", "adapter": "bap", "flow": "appointment",
  "status": "ON_CONFIRM", "network_id": "…", "counterparty_id": "…", "counterparty_uri": "…",
  "contract_id": "…", "coordination_id": "", "last_error": null,
  "routing": {"bap_id": "…", "bap_uri": "…", "bpp_id": "…", "bpp_uri": "…"},
  "context": {"bpp_id": "…", "bpp_uri": "…"},
  "actions": ["DISCOVER", "ON_DISCOVER", "SELECT", "ON_SELECT", "CONFIRM", "ON_CONFIRM"],
  "patient": null, "facility": null, "created_by": {…},
  "confirm": {"slot_id": "…", "patient": "…"},
  "created_date": "…", "modified_date": "…"
}
```

- `status` starts at `QUEUED`, becomes `<ACTION>` once ONIX ACKs the request and `ON_<ACTION>` when the callback
  arrives. `NACK` and `ERROR` carry `last_error`; a missing caller URL is `ERROR` with `last_error.code =
  "NOT_CONFIGURED"`.
- Referral transactions also carry `referral_id`, the origin Care `Referral`.
- For BAP referral detail and action routes, the origin referral UUID can also be used in place of
  `{transaction_id}`. Responses and network messages still contain the original transaction ID. This allows the
  referral's network status panel to find transactions created through discovery as well as Care-created referrals.
- On a successful `on_confirm`, `contract.contractAttributes.referral.id` is saved on the referral link as the
  remote referral ID, separately from `contract.id`. Subsequent automatic `update` messages include that remote
  ID in `contract.contractAttributes.referral.id`, the contract ID in `contract.id`, and the BPP routing from the
  callback. Existing links recover this ID from a retained successful callback when they next synchronize.
  If the callback does not include a remote referral ID, updates omit it for compatibility.
- The payload of a request or callback: `GET bap/message/?transaction_id=…&action=on_select` (`payload` holds the
  full Beckn body). Merged discover catalogs: `GET bap/<flow>/{transaction_id}/catalogs/`.

## Errors (CARE EMR shape)

| Case | Response |
|---|---|
| Invalid body (spec) | `400 {"errors": [<pydantic errors>]}` |
| Body is not an object / missing `transaction_id` filter | `400 {"errors": [{"type": "validation_error", "msg": …}]}` |
| Flow rule (e.g. appointment not confirmed yet, action not implemented) | `400 {"errors": [{"type": "beckn_error", "code": "70002", "msg": …}]}` |
| Referral `init` / `confirm` without the selected availability's request form | `400` `beckn_error` `70006` |
| Referral `questionnaire` not a request form of the selected availability, or a malformed `questionnaire_response` | `400` `beckn_error` `70007` |
| Unknown transaction, or one of another flow; unknown patient | `404 {"errors": [{"type": "object_not_found", "msg": …}]}` |
| Not authenticated | `401` / `403` |

Errors the receiving BPP returns on `on_select` / `on_init` / `on_confirm` (read them from `last_error`):

| Code | Meaning |
|---|---|
| `70005` | The facility does not accept network referrals: no active public availability, or it belongs to another facility |
| `70006` | The availability's request questionnaire was not answered |
| `70007` | The answers are invalid (`paths` lists the question ids), or the questionnaire changed: select again |

## FE change list (from the previous plugin API)

1. snake_case everywhere: bodies (`offer_id`, `slot_id`, `patient_name`, `context.bpp_id`) and responses
   (`transaction_id`, `contract_id`, `referral_id`).
2. The poll is the EMR retrieve; `?action=on_x` is gone: use `bap/message/?transaction_id=…&action=on_x`, or
   `catalogs/` for discover results.
3. Discover's `query` / `service_type` and referral select's `contract` fragment are gone.
4. Errors use the EMR `{"errors": [...]}` shape.
