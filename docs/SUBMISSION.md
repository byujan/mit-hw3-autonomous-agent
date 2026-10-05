# Homework 3 Submission — Autonomous Canvas Discussion Agent

**Student:** Peter (Byungwoo) Jang
**Repository:** https://github.com/byujan/mit-hw3-autonomous-agent (public)
**Forum:** https://canvas.mit.edu/courses/40577/discussion_topics/448963

---

## 1. Forum threads the agent participated in

Forum: [Homework 3: Agent Discussion Forum](https://canvas.mit.edu/courses/40577/discussion_topics/448963)

The agent autonomously chose both threads and wrote both replies; it was not
told which entries to answer or what to say.

**1. Autonomy-as-a-spectrum / persistent-memory thread** (top-level entry 229613)
→ **[agent's reply, entry 230081](https://canvas.mit.edu/courses/40577/discussion_topics/448963#entry-230081)**

Replies to the claim that the mitigation for memory pathologies is "selective
forgetting and periodic re-evaluation." The agent disagrees: forgetting is a
*write*, and an agent deciding unilaterally which of its memories are stale
performs an irreversible destructive operation on the only record that would
let it notice the decision was wrong. It proposes demotion with an audit trail
— mark an assumption low-confidence, keep the row — and connects this to the
neighbouring replies about unverified writes and unpinned paths.

**2. Intelligence-as-prediction thread** (top-level entry 229597)
→ **[agent's reply, entry 230082](https://canvas.mit.edu/courses/40577/discussion_topics/448963#entry-230082)**

Argues the separability question is aimed at the wrong layer: the model is the
interchangeable component and the surrounding state and protocol are what is
not. It then presses a link no one in the thread had challenged — what prevents
a double post after a dropped connection is not a better model of the future
but an unresolved intent record reconciled on the next cycle, so idempotence
is not prediction and is doing most of the work.

Both posts were confirmed saved by re-reading the thread after writing
(`verified=True`), and each thread contains exactly one reply from this agent.

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

Regenerate at any time with `python3 -m agent evidence -o var/evidence.md`.
Every cycle is recorded in the `runs` table with its outcome and the reason for
it, which is what makes a decision *not* to post auditable rather than
indistinguishable from a crash.

Cycles recorded at time of writing: **7** — `posted: 2`, `no_action: 5`.

| started (UTC) | trigger | outcome | posts | reason |
|---|---|---|---|---|
| 2026-10-05T15:40:15 | manual | no_action | 0 | dry run: replying to entry 229613 (score 6.9, 314 new items) |
| 2026-10-05T15:41:56 | manual | no_action | 0 | dry run: replying to entry 229613 (score 6.5) |
| 2026-10-05T15:42:39 | manual | no_action | 0 | dry run: replying to entry 229613 (score 6.5) |
| 2026-10-05T15:44:15 | manual | **posted** | 1 | replying to entry 229613 (score 6.5) → entry 230081 |
| 2026-10-05T15:44:58 | cron | **posted** | 1 | replying to entry 229597 (score 4.37, 1 new item) → entry 230082 |
| 2026-10-05T15:46:31 | cron | no_action | 0 | min spacing between posts not met (44 min remaining) |
| 2026-10-05T15:47:00 | cron | no_action | 0 | min spacing between posts not met (43 min remaining) |

**Deliberate decisions not to post.** The last two cron cycles are the agent
choosing silence while it had plenty it *could* have said: 12 top-level entries
were visible and several were unanswered, but it had already posted twice, so
the pacing gate held it back and it recorded why. Earlier cycles show the other
no-post paths — `AGENT_DRY_RUN=1` decides without writing, and the first cycle
registered 314 previously-unseen items while still posting only once.

The scheduler itself is installed and running:

```
$ crontab -l
7 */3 * * * /Users/peter/school/MIT-3/ai-studio/HW3/scripts/run_cycle.sh >> .../var/cron.log 2>&1
```

The cron path was verified in a stripped environment (`env -i`) to confirm it
loads `.env`, acquires the lock, and completes a full cycle with no inherited
shell state and no human input:

```
2026-10-05T15:46:59Z starting cycle
... control line: COURSE-TEAM CONTROL: RUNNING
... DECISION: no post this cycle -- min spacing between posts not met (43 min remaining)
2026-10-05T15:47:15Z cycle finished rc=0
```

**Additional pacing control.** The assignment's ceiling is three posts per hour.
During testing two cycles ran back to back and posted 90 seconds apart — within
the limit, but not representative of scheduled behaviour and spammy in a
discussion forum. A `min_minutes_between_posts` gate (default 45) was added on
top of the hourly cap; `tests/test_agent.py::test_min_spacing_blocks_rapid_second_post`
covers it.

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

**Test suite:** `python3 tests/test_agent.py` → 29 tests, all passing, offline.
Covers duplicate events, lost acknowledgements, HTTP 500/503 retry, 401
no-retry, timeouts, malformed JSON, restart persistence, the rate-limit cap,
post pacing, breaker open/reset, PAUSED compliance (including failing closed),
injection quarantine, and ignoring its own posts.

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
