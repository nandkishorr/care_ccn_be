# Frontend API

Routes are relative to `/api/care_ccn_be/`. URLs use the canonical flow names `referral` and `appointment`.
All FE routes need an authenticated user.

## Routes

| Old (core, under `/api/v1/beckn/`) | New | Called by |
|---|---|---|
| `POST bap/<action>` with `service_type` (discover) or `transactionId` (other actions) | `POST bap/<flow>/discover/`, `POST bap/<flow>/{transaction_id}/<action>/` | FE |
| `GET bap/transaction/<transaction_id>[?action=on_x]` | `GET bap/<flow>/{transaction_id}/[?action=on_x]` | FE |
| `POST bap/receiver[/<action>]` | `POST bap/receiver[/on_x][/]` | ONIX BAP adapter |
| `POST bpp/webhook[/<action>]`, `bpp/receiver[/<action>]` | `POST bpp/receiver[/x][/]` | ONIX BPP adapter |

The flow is in the URL, so `service_type` goes away. Only one implementation is active at a time: the plugin's flows stay
off while core `care.beckn` is installed.

## Request bodies

Every POST:

- may include `context` overrides, remembered for later actions. Allowed keys: `networkId`, `bppId`, `bppUri`,
  `schemaContext`, `ttl`; other keys are ignored. The FE sends `bppId` / `bppUri` once, at `select`, from the chosen
  catalog. They are dropped on discover, which always goes to the CDS;
- may include a passthrough `message`, validated against the flow's schema; the builder is skipped and the flow hooks
  still run;
- uses camelCase keys; snake_case variants (`offer_id`, `patient_name`, `transaction_id`) keep working;
- is rejected with `400` if it contains unknown fields.

`init` is optional in both flows: discover, select, confirm works.

Discover accepts `healthServiceType?`, `textSearch?`, `filters?` and today's `query` object. The per-flow bodies for
select, init, confirm, status, cancel and update are listed in `flows/<flow>/specs.py`.

## Responses

- Success: `202 {"transactionId", "messageId", "action", "status": "QUEUED"}`.
- Errors: `400` with `{"detail"}` or `{"errors": [...]}` (spec or flow validation); `404` for an unknown transaction or
  one that belongs to another flow.

## Poll response

`GET bap/<flow>/{transaction_id}/`

```json
{
  "transactionId": "a9046a23-4550-4cc1-b6ab-6aa5c86ce29f",
  "serviceType": "appointment",
  "flow": "appointment",
  "status": "ON_CONFIRM",
  "routing": {"bapId": "...", "bapUri": "...", "bppId": "...", "bppUri": "..."},
  "context": {"bppId": "...", "bppUri": "..."},
  "actions": ["SELECT", "ON_SELECT", "INIT", "ON_INIT", "CONFIRM", "ON_CONFIRM"],
  "patient": {"patient": "...", "facility": "..."},
  "contractId": "...",
  "error": null
}
```

- `status` starts at `QUEUED` and becomes `<ACTION>` only after ONIX ACKs the request, then `ON_<ACTION>` when the
  callback arrives. `NACK` and `ERROR` carry `error`. A missing caller URL is `ERROR` with `error.code = "NOT_CONFIGURED"`.
- Referral polls also include `referralId`, the id of the origin Care `Referral` (it was `resourceRequestId`).
- `?action=on_x` returns `{transactionId, status, action: "ON_X", ready, data}`. `?action=select` and the other request
  actions return what was sent. For `on_discover`, `data.message.catalogs` holds the catalogs of every responder.
- Transactions never expire. After the retention purge, payloads return `ready: false`.
- `404 {"detail": "Unknown transaction"}`.

## FE change list

1. Use the new URLs; drop `service_type` from discover and put the flow in the path.
2. Treat every POST as asynchronous: `202 QUEUED` means "stored". The old `result: ack | nack | error | skipped` is gone;
   poll for `status`, which becomes `NACK` or `ERROR` (with `error`) when ONIX rejects or delivery fails.
3. Show catalogs from every responder: `on_discover` is no longer last-writer-wins.
4. Stop treating a 404 as "expired"; transactions persist. A 404 now means unknown or wrong flow.
5. Handle `400` `{"errors": [...]}` for unknown fields (specs are strict).
