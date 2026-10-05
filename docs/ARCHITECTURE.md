# Architecture

## Cycle sequence

```
cron (every 3h, :07)
  │
  ├─ scripts/run_cycle.sh
  │    ├─ load .env            (secrets never on argv)
  │    ├─ refuse if no token
  │    └─ PID-file lock        (no overlapping cycles ⇒ no double-post)
  │
  └─ python3 -m agent run
       │
       ├─ 1. memory.start_run(trigger)
       │       └─ runs row: this cycle is now auditable even if it crashes
       │
       ├─ 2. breaker check ─────────────► open?  ⇒ outcome=blocked, exit
       │
       ├─ 3. reconcile_pending()
       │       for each intent in state='pending':
       │         re-read thread → match own post by normalised body hash
       │           found     ⇒ confirm_intent(verified=True)   [no re-post]
       │           not found ⇒ abandon_intent()                [may retry later]
       │
       ├─ 4. GET discussion_topics/{id}
       │       parse first line for "COURSE-TEAM CONTROL:"
       │       fail closed: only RUNNING permits a write
       │         PAUSED ⇒ outcome=paused, exit without writing
       │
       ├─ 5. GET entries, GET replies (paginated)
       │       mark every item seen; flag is_self
       │
       ├─ 6. decide()
       │       gates: rate limit → candidates → substance → injection screen
       │       outcome: reply | new_thread | nothing  (nothing is normal)
       │
       ├─ 7. two-phase write (only if acting)
       │       begin_intent(key = sha256(topic|action|parent|norm-body))
       │         ├─ duplicate key ⇒ idempotent stop
       │         └─ POST ──► Canvas
       │               ├─ success ⇒ re-read & verify ⇒ confirm_intent
       │               └─ failure ⇒ fail_intent, stays pending for step 3
       │
       └─ 8. record_success() | record_failure()
               5 consecutive failures ⇒ breaker opens 3h
```

## Why writes are at-most-once

A POST that fails with a network error is **ambiguous**: Canvas may have
committed the entry before the connection dropped. Retrying it is the single
easiest way to create duplicate posts, so the HTTP layer retries GETs freely
but never retries a write — except `429`, which means the request was rejected
outright and definitively wrote nothing.

That choice deliberately converts a duplicate-post risk into a *pending
intent*, which step 3 resolves on the next cycle by consulting Canvas as the
source of truth. The cost is one extra read after an interrupted write; the
benefit is that no failure mode produces a double post.

## Idempotency key

```
body_hash      = sha256("{action}|{parent_id}|{whitespace-normalised lowercased body}")
idempotency_key = sha256("{topic_id}|{action}|{parent_id}|{body_hash}")[:32]
```

Normalisation means Canvas rewriting the HTML of a post (adding `<p>` wrappers,
re-encoding entities) still matches during reconciliation, while genuinely
different content produces a different key.

## State model

| Table | Rows | Purpose |
|---|---|---|
| `seen_items` | one per entry/reply read | never reprocess; never engage with `is_self=1` |
| `intents` | one per intended write | `pending → confirmed \| abandoned`; the crash-recovery log |
| `posts` | one per confirmed post | rate-limit ledger + evidence links |
| `runs` | one per cycle | autonomy audit trail: outcome + reason |
| `breaker` | exactly one | consecutive failures, cooldown deadline |

`journal_mode=WAL` with `synchronous=FULL`, and every mutation is a single
`BEGIN IMMEDIATE` transaction, so `kill -9` mid-cycle leaves a consistent
database — the intent is either committed or absent, never half-written.

## Trust boundary

Everything from Canvas is untrusted data:

```
Canvas HTML
  → html_to_text()        strip tags, scripts, entities, zero-width/bidi chars
  → screen_for_injection() 14 patterns / 6 categories
       high severity (override, exfiltration, command, destructive, puppet)
         ⇒ quarantine: not replied to, not sent to the model, logged
  → fence()               wrap in <<<UNTRUSTED_FORUM_CONTENT>>> before the model
  → model output
  → scrub_outbound()      strip token shapes + invisibles before posting
```

The agent's capability set is the real control: the Canvas client has no update
or delete method, there is no shell execution, no file write outside `var/`, and
no network egress other than the one Canvas host. A successful injection can at
worst get the agent to post ordinary forum prose — it cannot leak the token,
touch another student's post, or reach the filesystem.
