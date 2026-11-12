# Architecture

`care_ccn_be` is the Beckn (NFH / DHP) BAP and BPP for Care. It replaces core's `care/beckn` app.

## Layers

```text
protocol/      pure Beckn: ack, context, builders, discover, HTTP client       (no Django, no Celery, no Care)
api/viewsets/  every HTTP API, on CARE's EMRBaseViewSet
               base.py: BecknBaseViewSet (BecknTransaction as the EMR resource, filters, schema, self-registration)
               base.py also provides BapViewSet / BppViewSet, action mixins and callback/request processing
               receiver.py: the two ONIX entry points;  message.py: the message list APIs
resources/     every spec: transaction/ and message/ (EMR read specs), bap/ (shared request/response specs),
               beckn/ (Beckn payload models, pure), <flow>/spec.py (request specs, state, retrieve spec) and schema.py
               builders/<flow>.py (contracts), catalog/<flow>.py (catalogs, when supported)
adapters/      shared BAP/BPP setting defaults; bap.py (identity/caller), bpp.py (identity/caller/publishing)
flows/<flow>/  flow.py (protocol identity), api.py (one viewset for BAP and BPP), Care lifecycle and helpers
               referral/__init__.py (referral setting defaults), referral/config.py (remote coordination endpoint)
constants.py   shared health vocabulary and booking constants
context.py     appointment and referral context builders (protocol/context.py stays framework-only)
models/        shared persistence models plus appointment.py and referral.py (flow link models)
signals/       appointment.py and referral.py (enabled-flow Care event receivers)
store.py       the ONLY writer of BecknTransaction and BecknMessage
tasks/         __init__.py (shared delivery, processing and housekeeping), appointment.py and referral.py (flow tasks)
services/      Care lookups shared by flows (patients, facilities, users, mappers, identifiers, publisher)
```

## APIs on CARE EMR

`BecknTransaction` is the EMR resource: `database_model`, `get_queryset` / `get_object` (lookup
`transaction_id`), `EMRListMixin` / `EMRRetrieveMixin` with `pydantic_read_model` / `pydantic_retrieve_model`, and
CARE's EMR exception handler (`BecknError` adds `{"type": "beckn_error", "code", "msg"}`).

Each Beckn action is one self-contained mixin, i.e. one API, like `EMRCreateMixin`:

| Member | BAP mixin (`Bap<Action>Mixin`) | BPP mixin (`Bpp<Action>Mixin`) |
|---|---|---|
| spec | `bap_<action>_spec` (request body) | `bpp_<action>_spec` (inbound `message`), checked before the ACK |
| authorization hook | `authorize_<action>(request_obj, txn)`, before anything is stored | `authorize_on_<action>(request_obj, beckn_request)`, before the ACK |
| `build_<action>` | `(request_obj, txn, state)`: the Beckn `message`, no side effects | |
| `perform` hook | `perform_<action>(request_obj, txn)`: stores the request; override + `super()` for side effects | `perform_on_<action>(request_obj, call)`: Care work, returns the `on_<action>` message (Celery) |
| `on_<action>` | `(call)`: runs in Celery when the callback arrives | |
| endpoint | `POST bap/<flow>/{transaction_id}/<action>/` (discover: `bap/<flow>/discover/`) | via `bpp/receiver/<action>` |

A flow viewset only lists the mixins it supports (adding one enables that API), declares `flow` and its specs, and
overrides hooks:

```python
class ReferralViewSet(
    BapDiscoverMixin, BapSelectMixin, BapInitMixin, BapConfirmMixin, BapStatusMixin,
    BppSelectMixin, BppInitMixin, BppConfirmMixin, BppStatusMixin, BecknViewSet,
):
    flow = ReferralFlow
    pydantic_retrieve_model = ReferralTransactionRetrieveSpec
    bap_select_spec = ReferralSelectSpec

    def build_select(self, request_obj, txn, state): ...
    def perform_select(self, request_obj, txn):
        msg = super().perform_select(request_obj, txn)
        ...  # side effects
        return msg
```

`api.viewsets.lookup.VIEWSETS` lists this class, and `adapters` decides which adapters it serves.
`urls.build_urlpatterns` serves it at the existing BAP and BPP prefixes.
`AdapterRouter` binds `adapter` per instance and exposes only list/retrieve on the BPP prefix; BAP action
endpoints remain on the BAP prefix. Background callbacks and inbound handlers also bind the adapter explicitly.

**Permissions** are not implemented. They plug into `get_queryset` (which transactions a user sees),
`authorize_retrieve`, and each mixin's authorization hook; raising `PermissionDenied` there returns `403` (BAP), and
a `BecknError` NACKs (BPP).

### ONIX

| Direction | URL (starter kit) | Setting / routing target |
|---|---|---|
| Care BAP to ONIX | `http://localhost:8081/bap/caller/<action>` | `BECKN_BAP_CALLER_URL=http://localhost:8081/bap/caller` |
| ONIX to Care BAP | `<care>/api/care_ccn_be/bap/receiver/on_<action>` | BAPReceiver routing `url: <care>/api/care_ccn_be/bap/receiver` |
| ONIX to Care BPP | `<care>/api/care_ccn_be/bpp/receiver/<action>` | BPPReceiver routing `url: <care>/api/care_ccn_be/bpp/receiver` |
| Care BPP to ONIX | `http://localhost:8082/bpp/caller/on_<action>` | `BECKN_BPP_CALLER_URL=http://localhost:8082/bpp/caller` |
| Care catalog to ONIX | `http://localhost:8082/catalog/publish` | `BECKN_CATALOG_PUBLISH_URL`; default: `BECKN_BPP_CALLER_URL` without `/bpp/caller` |

ONIX appends the action to the routing URL, and for v2 it routes on version and action only, so every flow's
requests of an action reach the same Care URL. The BPP receiver therefore picks the flow (known transaction, known
contract, then `BecknFlow.match`) and hands the request to that flow's BPP viewset.

Rules, enforced by tests (`tests/protocol/test_purity.py`, `tests/framework/test_registry_and_layers.py`):

- `protocol/` and `resources/beckn/` import nothing from Django, DRF, Celery or Care.
- Flows (and their `resources/<flow>/`) never import each other. Cross-flow reactions go through `events.py` signals.
- Only `store.py` writes `BecknTransaction` / `BecknMessage` through the ORM.
- The framework (`api/viewsets/`, `store.py`, `tasks/__init__.py`) never branches on a flow name.

## Data model

| Table | Purpose |
|---|---|
| `BecknTransaction` | One row per `(transaction_id, adapter)`. A loopback conversation has a BAP row and a BPP row. Holds status, counterparty routing, `contract_id`, `coordination_id`, the typed per-flow `state` JSON and `last_error`. |
| `BecknMessage` | Every request and callback in both directions: the audit log and the outbox in one table. Unique on `(message_id, action, direction, sender_id)`, which is what makes inbound retries idempotent. |
| `BecknLink` (abstract) | Each flow subclasses it to tie its Care rows (`Referral`, `TokenBooking`) to a transaction. |

Transaction `status` is `QUEUED`, `<ACTION>` (ONIX acknowledged the request), `ON_<ACTION>` (the callback arrived),
`NACK` or `ERROR`. There is no Redis: everything the old code kept for 24 h lives in these tables.

## Request lifecycles

**BAP (FE to ONIX).** `POST bap/<flow>/{transaction_id}/<action>/` validates `bap_<action>_spec`, gets the
transaction (EMR `get_object`), runs `authorize_<action>`, then in one database transaction: locks it, remembers the
context overrides and runs `perform_<action>` (passthrough `message` or `build_<action>`, then `record_outbound`,
status `QUEUED`). Delivery is queued `on_commit` and the FE gets `202`. `deliver_beckn_message` posts to ONIX and
records ACK / NACK / failure; a NACK or exhausted retry shows up as `NACK` / `ERROR`.

**ONIX callbacks to the BAP.** `bap/receiver` validates, dedupes, stores the inbound message and ACKs immediately.
`process_bap_callback` then applies it (`store.apply_callback`: counterparty, contract id, status) and runs the
`on_<action>` hook of the flow's BAP viewset. A callback only moves the status when it answers the latest request, so a late `on_select` cannot
rewind an `INIT`. A failing hook never loses the status update.

**ONIX requests to the BPP.** `bpp/receiver` validates, dedupes, routes to a flow (known transaction, then known
contract, then `BecknFlow.match` score), validates the flow's `bpp_<action>_spec` and runs
`authorize_on_<action>` (NACK on failure), stores and ACKs. `process_bpp_request` runs `perform_on_<action>` of the flow's BPP viewset and queues
the callback; a `BecknError` becomes an error callback, any other exception becomes `PROCESSING_FAILED`.

**Unsolicited callbacks.** Care-initiated `on_status` / `on_update` go through `store.record_unsolicited`.

**Reliability.** Every send is persisted first and delivered after commit. `requeue_stuck_messages` (every 5 min)
re-queues anything stuck for 10 minutes; `purge_beckn_messages` enforces `BECKN_MESSAGE_RETENTION_DAYS`.

## Flows and lookups

A flow is a `BecknFlow` subclass in `flows/<name>.py` holding the protocol identity only: name, aliases, context
builder, typed state, catalog builder, `match` and `coordination_id`.

There is no registry. `apps.ready()` imports the viewsets, `tasks/<name>.py` (so Celery knows the tasks even for
disabled flows) and `signals/<name>.py`. Every receiver checks `is_flow_enabled(...)` itself, so receivers of
disabled flows are connected but do nothing. Code that knows its flow (tasks, signals) imports the flow class.
Name lookups remain only where the name comes from data:

- `flows/lookup.py`: `FLOWS`, `get_flow` / `find_flow` (alias-aware), `enabled_flows`, `is_flow_enabled` and
  `match_flow`. `BecknTransaction.flow` and `BECKN_ENABLED_FLOWS` store names, and a first BPP request has no
  transaction, so it is matched against the enabled flows.
- `api/viewsets/lookup.py`: `VIEWSETS` and `get_viewset(adapter, name)`, which honours the viewset's `adapters`.

URLs are built from the enabled flows (`urls.build_urlpatterns`). Receiver dispatch IDs and Celery task names are
unchanged.

Care core must not also ship its own `care.beckn` app: the plugin does not coexist with it.

### Adding a flow

1. Create `resources/<name>/spec.py` (request specs, typed state, retrieve spec) and `schema.py` (payload models).
2. Create `resources/builders/<name>.py` and, if the flow publishes catalogs, `resources/catalog/<name>.py`.
   Create `flows/<name>.py` and add it to `flows.lookup.FLOWS`. Create one viewset in
   `api/viewsets/<name>.py` inheriting `BecknViewSet`, and add it to `api.viewsets.lookup.VIEWSETS`.
   Add `Bap<Action>Mixin`s (`bap_<action>_spec`, `build_` / `perform_` / `on_<action>`) and
   `Bpp<Action>Mixin`s (`bpp_<action>_spec`, `authorize_on_` / `perform_on_`), declaring `flow = ...`.
   Set `adapters = ("bap",)` or `("bpp",)` when Care only plays one adapter for the flow.
   Put optional tasks in `tasks/<name>.py` and signal receivers in `signals/<name>.py`, and import both in
   `apps.ready()`. Put link models
   (a `BecknLink` subclass) in `models/<name>.py`. Keep shared constants and context builders in `constants.py`
   and `context.py`.
3. Import the link models in `models/__init__.py` and run `makemigrations`.
4. Add the name to `BECKN_ENABLED_FLOWS` and document the routes in `docs/fe_api.md`.

Nothing in `protocol/`, `api/`, `store.py`, `tasks/__init__.py`, `urls.py` or the ONIX configuration changes.

### Adding an action

Add its constant and `ActionSpec` in `protocol/actions.py`, the `Bap<Action>Mixin` and `Bpp<Action>Mixin` in
`api/viewsets/base.py`, and mix them into the flows that support it.

## Payload schema

Every Beckn payload is a Pydantic model; builders and handlers set and read typed fields, never raw dict keys.
Python is snake_case, the wire is camelCase (`coordination_ref` <-> `coordinationRef`), and unknown fields are kept
and echoed, so the ONIX adapter stays the authority on what a network accepts.

| Where | What goes there |
| --- | --- |
| `resources/beckn/core.py`, `envelope.py` | Beckn v2 core: `Contract`, `Commitment`, `Participant`, `Performance`, `Catalog`, `Intent`, context |
| `resources/beckn/codes.py` | DHP code lists (`HealthServiceType`, `Gender`, `UrgencyTier`, ...) |
| `resources/beckn/health.py` | One model per DHP v2.1 `*Attributes` schema (`HealthContract`, `HealthPerformance`, `HealthParticipant`, `HealthResource`, `HealthOffer`, `ServiceCoordinationResource`) on the spec's `Service*` bases |
| `resources/<flow>/schema.py` | Flow vocabulary (`HealthReferral`) and per-action messages (`AppointmentSelectMessage`, `ReferralConfirmMessage`) with their required-field validators |

To carry a new field, declare it on the model that owns it, e.g. `booking_channel: Code[BookingChannel] | None` on
`ServiceContract`; it is then parsed, validated and emitted wherever that model is used. A new `*Attributes` object is
a `JsonLdModel` subclass with `LD_TYPE` / `LD_CONTEXT`: it registers itself and inbound objects with that `@type`
parse into it. A field only one action needs (for example `intent` on appointment `select`) goes on that action's
message model, and the viewset points `on_<action>_spec` at it. `Code[...]` fields map known values to the enum
and pass other strings through; types, dates, nested objects and validators are enforced, and an invalid inbound
message is NACKed with `70002`.

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

The referral flow stores its rows in Care's first-class referral domain.

| Concern | Where it lives |
|---|---|
| The referral | `emr.Referral`, tied to a transaction by `ReferralLink(referral, role)`; idempotent on (coordination id, role) |
| Where it goes | `ReferralAvailability`. A *network* one (`entity_type=external`, `external_entity["beckn"] = {bppId, bppUri, assignedFacilityId?}`) is the Beckn target; a *facility* one receives network referrals and its `care_team` becomes the `assignees` |
| What `Referral` has no column for | `ReferralLink.details` (`urgency`, `originFacilityId`, `contact`) |
| Referral type | `consultation` is `CONSULTATION` in `targetCriteria`, `lab` is `INVESTIGATION`; the FE categories map `patient_care` to consultation and everything else to lab |
| Status | `pending` is DRAFT, `in_progress` is ACTIVE, `completed` is FULFILLED, `cancelled` and `rejected` are CANCELLED |

* **Care to network:** a new pending `Referral` on a network availability whose type is in `BECKN_REFERRAL_TYPES`
  is sent to that availability's target as a `confirm` (`signals/referral.py`).
* **Network to Care:** the BPP creates the origin referral on the coordination centre's network availability and
  the assigned one on the receiving facility's availability (`services/availability.py`).
* **Patient:** Care requires one, so a payload or BAP request without a patient participant or `patient` /
  `patientName` is an error callback (BPP) or a failed message (BAP callback).
* **Authorization:** Care hides `external` availabilities; `authz.BecknReferralAccess` (an override controller that
  extends `ReferralAccess`) lists and shows public, active network availabilities.
* **Setup:** `manage.py beckn_referral_availability` creates the network availabilities of the coordination centre
  (or of `--bpp-id/--bpp-uri`). The API refuses to create `external` availabilities, so the plugin owns them.
