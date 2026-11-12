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
| `ReferralInitSpec`, `ReferralConfirmSpec` | `facility*` (uuid), `patient` (uuid) or `patient_name`, `encounter` (uuid), `title`, `reason`, `category`, `offer_id`, `assigned_facility_id`, `emergency`, `priority` |
| `AppointmentSelectSpec` | `resource_id*`, `offer_id`, `date`, `health_service_type` |
| `AppointmentInitSpec` | `slot_id*`, `patient*` (uuid) |
| `AppointmentConfirmSpec` | `slot_id*`, `patient*` (uuid), `coordination_ref` |
| `AppointmentRescheduleSpec` (`update`) | `slot_id*` |
| `AppointmentCancelSpec` | `reason` |
| `BapActionSpec` (`status`) | only `context` / `message` |

`init` is optional in both flows: discover, select, confirm works.

A referral `encounter` stays in Care (it is not sent to the network): the origin `Referral` is linked to it, and its
patient is used when `patient` is omitted. It must belong to `patient` and `facility`, else `400`; unknown is `404`.

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
- The payload of a request or callback: `GET bap/message/?transaction_id=…&action=on_select` (`payload` holds the
  full Beckn body). Merged discover catalogs: `GET bap/<flow>/{transaction_id}/catalogs/`.

## Errors (CARE EMR shape)

| Case | Response |
|---|---|
| Invalid body (spec) | `400 {"errors": [<pydantic errors>]}` |
| Body is not an object / missing `transaction_id` filter | `400 {"errors": [{"type": "validation_error", "msg": …}]}` |
| Flow rule (e.g. appointment not confirmed yet, action not implemented) | `400 {"errors": [{"type": "beckn_error", "code": "70002", "msg": …}]}` |
| Unknown transaction, or one of another flow; unknown patient | `404 {"errors": [{"type": "object_not_found", "msg": …}]}` |
| Not authenticated | `401` / `403` |

## FE change list (from the previous plugin API)

1. snake_case everywhere: bodies (`offer_id`, `slot_id`, `patient_name`, `context.bpp_id`) and responses
   (`transaction_id`, `contract_id`, `referral_id`).
2. The poll is the EMR retrieve; `?action=on_x` is gone: use `bap/message/?transaction_id=…&action=on_x`, or
   `catalogs/` for discover results.
3. Discover's `query` / `service_type` and referral select's `contract` fragment are gone.
4. Errors use the EMR `{"errors": [...]}` shape.
