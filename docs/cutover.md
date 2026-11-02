# Cutover and backfill

Core's `care.beckn` and this plugin never run at the same time: the plugin stays passive (warning
`care_ccn_be.W001`) while `care.beckn` is installed. Disabling the core app is the switch, in both directions.

## `manage.py beckn_backfill`

```text
beckn_backfill [--dry-run] [--since YYYY-MM-DD] [--only referral|appointment|redis] [--strip-legacy [--force]]
```

Legacy referrals are `ResourceRequest` rows; the backfill turns each into a Care `Referral` (the request is left
untouched and kept on `ReferralLink.legacy_resource_request`). A request without a patient cannot become a referral and
is counted *skipped*. The origin referral goes on the coordination centre's network availability, the assigned one on
the assigned facility's availability; both are created on demand (`manage.py beckn_referral_availability` pre-creates
the network ones).

Idempotent (rows keyed by `(transaction_id, side)` and the link constraints), batched (500 rows per DB transaction),
never calls ONIX, never deletes. Backfilled transactions carry `state.legacy = {"source", "at"}`. It prints created /
existing / ambiguous / skipped per shape. `--dry-run` runs everything and rolls each batch back, so the Redis shapes
that depend on earlier shapes (6 after 5) report lower counts than a real run.

| Shape | Legacy data | Becomes |
|---|---|---|
| `referral_confirm` | `ResourceRequest.extensions["beckn"]` with `role` and `returnRouting` | BPP referral transaction (`ON_CONFIRM`), a `Referral` + `ReferralLink`, and a synthetic inbound `confirm` holding the stored contract |
| `referral_init` | blob with `role` and `transactionId`, no `returnRouting` | BAP transaction if Redis still has the record, else BPP (counted *ambiguous*); `ON_INIT` + `ReferralLink` |
| `referral_on_confirm` | blob with `transactionId` + `contract`, no `role` | BAP transaction (counterparty = coordination center, `ON_CONFIRM`) + origin `ReferralLink` |
| `referral_sent` | request with no blob that Care itself sent (RR id = transaction id) | BAP transaction, a `Referral` and the origin link. Needs `--since`; category in `BECKN_REFERRAL_CATEGORIES` (legacy requests only) and still open |
| `appointment_booking` | `TokenBooking.meta["beckn"]` | BPP appointment transaction, `AppointmentLink`s (reschedule copies share one transaction; the newest non-rescheduled booking is the active link) and a synthetic inbound `confirm` |
| `redis_booking_referral` | `beckn:booking-referral:<booking id>` | `coordination_id` of that booking's transaction |
| `redis_transaction` | `beckn:txn:<id>` and `beckn:txn:<id>:<ACTION>` | BAP transaction plus one message per stored payload; only matters in the catch-up run |

`Patient.extensions["beckn"]` needs nothing: the extension keeps `{v: 1, primaryLanguage}`.

`--strip-legacy` (after the rollback window) refuses while a legacy blob has no link, unless `--force`. It reduces
`ResourceRequest.extensions["beckn"]` to `{v: 1, target?}`, `Patient.extensions["beckn"]` to `{v: 1, primaryLanguage?}`
and removes `"beckn"` from `TokenBooking.meta`, using `QuerySet.update()` so no signal fires and nothing is sent.

## Runbook

**Before (no downtime)**
1. Deploy core with the `BECKN_CORE_ENABLED` gate on (behavior unchanged).
2. Add the plugin to `plug_config.py`, deploy, `migrate`. The plugin is passive.
3. `beckn_backfill --dry-run --since <go-live date>`; review ambiguous rows and counts.
4. `beckn_backfill --since <go-live date>` (bulk; safe while core is live).
5. Prepare the FE release ([fe_api.md](fe_api.md)), the ONIX target change for both adapters, and
   `BECKN_SHARED_SECRET` / `BECKN_ALLOWED_IPS`.

**Window (minutes)**
6. Stop Celery beat and wait until no `care.beckn.tasks.*` task is active, reserved or scheduled.
7. Disable core's `care.beckn`, restart web, workers and beat. W001 is gone and the plugin routes resolve.
8. Run `beckn_backfill --since …` again (catch-up, plus live Redis).
9. Point both ONIX adapters at the plugin receivers and release the FE.
10. Smoke test in the sandbox: BPP `init` and `confirm` get an immediate ACK and the callbacks reach ONIX; a repeated
    `confirm` returns the same ACK and leaves one booking; BAP discover, select, init, confirm reaches `ON_CONFIRM`;
    cancelling the booking in Care sends `on_status`; a new referral is sent and the coordination center's
    `on_confirm` approves it.

**After**
11. For a week, watch FAILED and NACKED messages in the admin, requeue counts and the poll error rate.
12. When the rollback window closes, run `beckn_backfill --strip-legacy`.

## Rollback

| From | Steps | What is lost |
|---|---|---|
| Before step 7 | nothing; the plugin stays passive | nothing |
| After step 7, before `--strip-legacy` | re-enable core `care.beckn`, restart, revert the ONIX targets and the FE | core does not see conversations started on the plugin (no legacy blobs), so it sends them no `on_status` or completion `on_update`; their rows stay in the plugin tables |
| After `--strip-legacy` | not supported | core has lost the blobs it reads |

Requests that arrive between steps 7 and 9 get a 404; whether ONIX retries them decides how short that gap must be.

## To verify in the sandbox

- ONIX accepts a callback that reuses the request's `messageId` (`BECKN_CALLBACK_REUSE_MESSAGE_ID=False` restores the
  old behavior).
- ONIX retry behavior on a timeout, 5xx or 404.
- ONIX can send a static `X-Beckn-Secret` header; otherwise use the IP allowlist.
