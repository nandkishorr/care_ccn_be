# Architecture

`care_ccn_be` is the Beckn (NFH / DHP) BAP and BPP for Care. It replaces core's `care/beckn` app.

## Layers

```text
protocol/   pure Beckn: schema, ack, context, builders, discover, HTTP client   (no Django, no Celery, no Care)
flows/      one folder per use case (referral, appointment): the only code that knows Care models
bap/        FE-facing viewsets (per-action mixins), the pipeline, the ONIX callback receiver
bpp/        the ONIX request receiver and per-flow services (per-action mixins)
store.py    the ONLY writer of BecknTransaction and BecknMessage
tasks.py    Celery: deliver, process_bpp_request, process_bap_callback, requeue, purge
services/   Care lookups shared by flows (patients, facilities, users, mappers, identifiers, publisher)
```

Rules, enforced by tests (`tests/protocol/test_purity.py`, `tests/framework/test_registry_and_layers.py`):

- `protocol/` imports nothing from Django, DRF, Celery or Care.
- Flows never import each other. Cross-flow reactions go through `events.py` signals.
- Only `store.py` writes `BecknTransaction` / `BecknMessage` through the ORM.
- The framework (`bap/`, `bpp/`, `store.py`, `tasks.py`) never branches on a flow name.

## Data model

| Table | Purpose |
|---|---|
| `BecknTransaction` | One row per `(transaction_id, side)`. A loopback conversation has a BAP row and a BPP row. Holds status, counterparty routing, `contract_id`, `coordination_id`, the typed per-flow `state` JSON and `last_error`. |
| `BecknMessage` | Every request and callback in both directions: the audit log and the outbox in one table. Unique on `(message_id, action, direction, sender_id)`, which is what makes inbound retries idempotent. |
| `BecknLink` (abstract) | Each flow subclasses it to tie its Care rows (`Referral`, `TokenBooking`) to a transaction. |

Transaction `status` is `QUEUED`, `<ACTION>` (ONIX acknowledged the request), `ON_<ACTION>` (the callback arrived),
`NACK` or `ERROR`. There is no Redis: everything the old code kept for 24 h lives in these tables.

## Request lifecycles

**BAP (FE to ONIX).** `POST bap/<flow>/<txn>/<action>/` goes through `dispatch_beckn_action`: validate the spec, lock the
transaction, remember context overrides, build the context, build the message (or take the passthrough `message`),
`record_outbound` (status `QUEUED`), and queue delivery `on_commit`. The FE gets `202`. `deliver_beckn_message` posts to
ONIX and records ACK / NACK / failure; a NACK or exhausted retry shows up in the poll as `NACK` / `ERROR`.

**ONIX callbacks to the BAP.** `bap/receiver` validates, dedupes, stores the inbound message and ACKs immediately.
`process_bap_callback` then applies it (`store.apply_callback`: counterparty, contract id, status) and runs the flow's
`on_<action>` hook. A callback only moves the status when it answers the latest request, so a late `on_select` cannot
rewind an `INIT`. A failing hook never loses the status update.

**ONIX requests to the BPP.** `bpp/receiver` validates, dedupes, routes to a flow (known transaction, then known
contract, then `BecknFlow.match` score), stores and ACKs. `process_bpp_request` runs `bpp_<action>` of the flow's service
and queues the callback; a `BecknError` becomes an error callback, any other exception becomes `PROCESSING_FAILED`.

**Unsolicited callbacks.** Care-initiated `on_status` / `on_update` go through `store.record_unsolicited`.

**Reliability.** Every send is persisted first and delivered after commit. `requeue_stuck_messages` (every 5 min)
re-queues anything stuck for 10 minutes; `purge_beckn_messages` enforces `BECKN_MESSAGE_RETENTION_DAYS`.

## Flows and the registry

A flow is a `BecknFlow` subclass in `flows/<name>/flow.py`, registered with `@FlowRegistry.register`. It declares its
context builder, typed state, BAP viewset, BAP callbacks, BPP service and catalog builder. `FlowRegistry.autodiscover()`
runs in `apps.ready()`; it imports each flow's `flow.py` and `tasks.py`, and `signals.py` only for **enabled** flows
(`BECKN_ENABLED_FLOWS`). URLs are built from the enabled flows (`urls.build_urlpatterns`).

The plugin is passive while core's `care.beckn` is installed: `FlowRegistry.enabled()` is empty and `manage.py check`
reports `care_ccn_be.W001`.

### Adding a flow

1. Create `flows/<name>/` with `flow.py` (register the flow), `schema.py`, `builders.py`, `specs.py`, and a BAP viewset
   and/or BPP service composed from the per-action mixins (`bap/mixins`, `bpp/mixins`). Optional: `tasks.py`,
   `signals.py`, `models.py` (a `BecknLink` subclass).
2. Import the link models in `models/__init__.py` and run `makemigrations`.
3. Add the name to `BECKN_ENABLED_FLOWS` and document the routes in `docs/fe_api.md`.

Nothing in `protocol/`, `bap/`, `bpp/`, `store.py`, `tasks.py`, `urls.py` or the ONIX configuration changes.

### Adding an action

Add its constant and `ActionSpec` in `protocol/actions.py`, a mixin in `bap/mixins` and `bpp/mixins`, and mix it into
the flows that support it. A BPP service accepts exactly the actions whose `bpp_<action>` handler it has.

## Security

Both receivers use `BecknReceiverPermission`: a CIDR allowlist (`BECKN_ALLOWED_IPS`) and/or a shared secret in the
`X-Beckn-Secret` header (`BECKN_SHARED_SECRET`, compared in constant time). With neither set the receivers are open and
the app logs a warning at start-up.

## Signals and re-entrancy

Plugin-initiated writes to Care rows are wrapped in `suppress_beckn_signals()` (a `ContextVar`) so the plugin's own saves
never trigger the Beckn signal receivers. Reschedule detection state is a `ContextVar` reset on every request and task.

## Testing

Tests run inside a Care checkout with the plugin installed (`python manage.py test care_ccn_be`). Framework tests use a
test-only `EchoFlow` and a loopback that routes Care's outbound ONIX calls into its own receivers, with Celery eager and
`on_commit` inline, so a whole BAP, ONIX, BPP, ONIX, BAP loop runs synchronously.

## Referrals on Care's `Referral`

The referral flow stores its rows in Care's first-class referral domain, not in `ResourceRequest`.

| Concern | Where it lives |
|---|---|
| The referral | `emr.Referral`, tied to a transaction by `ReferralLink(referral, role)`; idempotent on (coordination id, role) |
| Where it goes | `ReferralAvailability`. A *network* one (`entity_type=external`, `external_entity["beckn"] = {bppId, bppUri, assignedFacilityId?}`) is the Beckn target; a *facility* one receives network referrals and its `care_team` becomes the `assignees` |
| What `Referral` has no column for | `ReferralLink.details` (`urgency`, `originFacilityId`, `contact`) |
| Referral type | `consultation` is `CONSULTATION` in `targetCriteria`, `lab` is `INVESTIGATION`; the FE categories map `patient_care` to consultation and everything else to lab |
| Status | `pending` is DRAFT, `in_progress` is ACTIVE, `completed` is FULFILLED, `cancelled` and `rejected` are CANCELLED |

* **Care to network:** a new pending `Referral` on a network availability whose type is in `BECKN_REFERRAL_TYPES`
  is sent to that availability's target as a `confirm` (`flows/referral/signals.py`).
* **Network to Care:** the BPP creates the origin referral on the coordination centre's network availability and
  the assigned one on the receiving facility's availability (`services/availability.py`).
* **Patient:** Care requires one, so a payload or BAP request without a patient participant or `patient` /
  `patientName` is an error callback (BPP) or a failed message (BAP callback).
* **Authorization:** Care hides `external` availabilities; `authz.BecknReferralAccess` (an override controller that
  extends `ReferralAccess`) lists and shows public, active network availabilities.
* **Setup:** `manage.py beckn_referral_availability` creates the network availabilities of the coordination centre
  (or of `--bpp-id/--bpp-uri`). The API refuses to create `external` availabilities, so the plugin owns them.

