# Engineering decisions

**1. Append-only `events` table + current-state `items` table.** Items hold the current state for fast list queries; every change (create, claim, assign, status, edit, comment) writes an immutable event in the *same transaction*. History scales independently (indexed `(item_id,id DESC)`, paged 50 at a time) and "why did this happen" is always answerable. Trade-off: two writes per action; no event-sourced replay.

**2. Concurrency: CAS for claims, optimistic versioning for edits, idempotency keys for retries.**
- *Claim*: `UPDATE … WHERE assignee_id IS NULL` — the database decides the single winner; losers get 409 with the current owner. Re-claim by the same user is a no-op 200.
- *Edit/assign/transition*: client sends the `version` it saw; `WHERE version=?` fails with 409 + current item if stale. Chosen over locks (no abandoned locks) and over last-write-wins (silent loss).
- *Retries*: `Idempotency-Key` stored with the response inside the same transaction as the effect, so a repeated request returns the original result and never creates duplicates.
- Frontend reconciles by always re-fetching server truth after any action (optimistic hint on claim, rolled back by refresh on 409).

**3. Authorization is resource-level and server-side.** Per-team roles (viewer < member < manager < admin). Every handler loads the item, checks the caller's role *in that item's team*. Non-members get 404 (no existence leak). Rules beyond role: members may only move status on items they own; priority changes and assigning others need manager; `needs_approval` items cannot reach `done` without a manager. All list/summary queries are scoped by membership in SQL.

**4. Workflow as an explicit state machine** (`open→in_progress→review→done`, cancel/back transitions, terminal states). Invalid jumps are 422; stale transitions are 409. Keeping it a table makes it easy to extend per team later.

**5. Transactional outbox for async work.** Notifications are not created in the request path. The event + an `outbox` row commit atomically; a worker delivers with retry/exponential backoff and a dead state after 5 attempts. Delivery is at-least-once, and effects are idempotent (`UNIQUE(event_id,user_id)`), so crashes/duplicates are harmless. Trade-off: notifications are delayed ~1s; the request never fails because of notification problems.

**Scale/search:** keyset pagination `(updated_at,id)` (no OFFSET), composite indexes, server-side filters, an attention query capped at 30 rows, so the browser never holds the dataset. **Not built (deliberately):** real authN, push updates, FTS, multi-node deployment — see README.

**Tests target the dangerous bits:** concurrent claim, stale update, duplicate create, authorization matrix, workflow/approval rules, outbox retry + redelivery, pagination.
