# Homework 3 Submission — Autonomous Canvas Discussion Agent

**Student:** Peter (Byungwoo) Jang
**Repository:** https://github.com/byujan/mit-hw3-autonomous-agent

> Fill in the bracketed sections after the agent has completed several
> scheduled cycles. Generate the run table with:
> `python3 -m agent evidence -o var/evidence.md`

---

## 1. Forum threads the agent participated in

[Paste the live Canvas links here — `python3 -m agent status` prints a
verified URL for every post the agent made.]

- [ ] Thread / reply 1 — <link>
- [ ] Thread / reply 2 — <link>

## 2. Code

https://github.com/byujan/mit-hw3-autonomous-agent — setup instructions in `README.md`.
Python 3.11+, standard library only. No token or local state is committed.

## 3. Architecture and autonomy

Full detail in `README.md` and `docs/ARCHITECTURE.md`. Summary:

- **Scheduler** — `cron` runs `scripts/run_cycle.sh` every 3 hours at :07. The
  script loads `.env`, refuses to run without a token, and holds a PID-file lock
  so cycles cannot overlap. Each invocation is a complete independent cycle with
  no human input.
- **Canvas access** — stdlib REST client against `https://canvas.mit.edu`, token
  from the environment as a Bearer header. Reads topic/entries/replies with
  pagination; the only writes are `create_entry` and `create_reply`. There is no
  update or delete capability in the client at all.
- **Decision logic** — entries are scored on substance, novelty, how
  under-served the thread is, and whether they ask an answerable question. The
  agent replies to at most one entry per cycle, and skips its own posts, already
  answered entries, sub-120-character posts, and quarantined injection attempts.
  It opens a new thread only if the forum has substantive discussion, nothing
  was posted in 6 hours, and 24 hours have passed since its last thread.
  Otherwise it records `no_action`.
- **Persistent local memory** — SQLite (`var/memory.db`, WAL, `synchronous=FULL`):
  `seen_items` (with `is_self`), `intents` (two-phase write log), `posts`
  (durable ledger), `runs` (per-cycle audit), `breaker`.
- **Verification** — after each write the agent re-reads the thread and confirms
  its new entry id is present before marking the post `verified=1`.
- **Rate limits** — ≤3 posts/hour enforced from the durable ledger (survives
  restarts), ≤1 post/cycle, ≤1 new thread/24h. Read retries use exponential
  backoff with jitter and honour `Retry-After`; writes are never blindly retried.
- **Stopping rule** — a circuit breaker opens for 3 hours after 5 consecutive
  failed cycles; later cycles exit immediately as `blocked`. Success resets it.
- **Pause compliance** — the discussion topic is re-fetched before every write
  and the `COURSE-TEAM CONTROL:` line parsed. It fails closed: only `RUNNING`
  permits posting; missing, malformed, or misplaced control lines count as PAUSED.

## 4. Activity evidence (multiple scheduled runs, including a deliberate no-post)

[Paste the table from `var/evidence.md`.]

The `runs` table records every cycle with its outcome and reason. Cycles with
outcome `no_action` are the agent deliberately choosing not to post, with the
reason recorded — e.g. `nothing useful to add (0 new items, 3 substantive,
0 quarantined, 3 skipped)` when every open entry had already been answered.

## 5. Failure and recovery evidence

Reproduce with `python3 scripts/failure_demo.py` (offline, no token required);
transcript saved to `var/failure_demo.md`. Verified output:

**Injected failure — lost acknowledgement.** The agent POSTs a reply; the fake
Canvas commits it, then raises a connection error before the response is read.

```
## Cycle 1 -- acknowledgement is swallowed mid-write
  cycle outcome   : error (network error on .../discussion_topics/448963/)
  replies ON CANVAS: 1  <-- Canvas did commit it
  local memory after cycle 1:
    confirmed posts : 0
    pending intents : 1
      - 862fcfebe376 state=pending attempts=1

## Cycle 2 -- recovery with a healthy connection
  cycle outcome   : no_action
  replies ON CANVAS: 1  <-- still ONE, no duplicate
  local memory after cycle 2:
    confirmed posts : 1
    pending intents : 0
      + canvas_id=1002 verified=True

  duplicate-free: PASS
```

**Recovery behaviour.** The orphaned `pending` intent is reconciled on the next
cycle: the agent re-reads the thread, matches its own post by normalised body
hash, and confirms it locally rather than posting again. Completed work is
preserved; no duplicate is created.

**Protection against duplicate effects.** Three layers — writes are at-most-once
at the HTTP layer (a POST is never blindly retried, since a network error may
follow a successful commit; only `429` is retried); deterministic idempotency
keys make a repeated intent a no-op; and reconciliation resolves unknown
outcomes against Canvas as the source of truth.

**Stopping rule, same run:**

```
## Cycle 3 -- repeated hard failures must stop the agent
  outcome: error -- HTTP 500 ...   (x3)
  breaker: failures=3 open=True
  next cycle refuses to run: blocked (circuit breaker open until ...)
  stopping-rule: PASS
```

**Test suite:** `python3 tests/test_agent.py` → 28 tests, all passing, offline.
Covers duplicate events, lost acknowledgements, HTTP 500/503 retry, 401
no-retry, timeouts, malformed JSON, restart persistence, the rate-limit cap,
breaker open/reset, PAUSED compliance (including failing closed), injection
quarantine, and ignoring its own posts.

## 6. Credential and privacy hygiene

- The Canvas token is read only from the environment (`.env`, gitignored,
  `chmod 600`) and sent only as an `Authorization` header — never in argv, a
  URL, the database, or a log.
- All logging passes a redacting filter that scrubs token-shaped strings,
  `Authorization:` headers, and `access_token=` parameters.
- `scrub_outbound()` strips token-shaped text and invisible characters from
  every message before it is posted.
- `var/` (memory database, logs, local state) is gitignored; the repository
  contains no `.env`, credentials, grades, or personal data.
