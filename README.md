# Homework 3 — Autonomous Canvas Discussion Agent

An autonomous agent that participates in the **Homework 3: Agent Discussion Forum**
on `canvas.mit.edu` without anyone prompting it. It runs on a cron schedule,
decides for itself whether it has anything worth saying, remembers what it has
already seen and posted across restarts, verifies that each contribution was
actually saved, and stops itself when things go wrong.

Python 3.11+, standard library only. No framework, no network dependencies.

---

## Quick start

```bash
git clone <this repo>
cd HW3

# 1. Credentials (never committed)
cp .env.example .env
chmod 600 .env
$EDITOR .env          # paste your Canvas token; set course/topic ids

# 2. Verify connectivity and discover ids
python3 -m agent bootstrap

# 3. Dry run — decides but never writes to Canvas
AGENT_DRY_RUN=1 python3 -m agent run --trigger manual

# 4. Install the schedule (every 3 hours)
./scripts/install_cron.sh 3

# 5. Observe
python3 -m agent status
tail -f var/cron.log
```

Create the token at **Canvas → Account → Settings → Approved Integrations →
+ New Access Token**, with an expiration shortly after the due date. It is read
only from the environment; it never appears in source, output, or the database.

## Commands

| Command | Purpose |
|---|---|
| `python3 -m agent bootstrap` | Verify the token, auto-discover course/topic ids, print the control line |
| `python3 -m agent run` | Execute one autonomous cycle (what cron calls) |
| `python3 -m agent status` | Memory contents, rate-limit budget, breaker state, run history |
| `python3 -m agent evidence -o var/evidence.md` | Generate the activity-evidence table |
| `python3 -m agent reset-breaker` | Clear the breaker after fixing a problem |

## Architecture

```
cron (every 3h)
  └─> scripts/run_cycle.sh        PID-file single-flight, loads .env, never passes
      └─> python3 -m agent run        secrets on argv
          └─> agent/runner.run_cycle()
                1. memory.start_run()                  — audit row for this cycle
                2. reconcile_pending()                 — recover interrupted writes
                3. GET discussion topic → control line — PAUSED ⇒ stop, no writes
                4. GET entries + replies               — update the seen-set
                5. decide()                            — may choose to do nothing
                6. begin_intent → POST → verify → confirm_intent
                7. record_success / record_failure     — breaker accounting
```

| Module | Responsibility |
|---|---|
| `agent/config.py` | Environment-only configuration; hard-caps posts/hour at 3 |
| `agent/canvas.py` | REST client: pagination, timeouts, backoff; **no update/delete methods** |
| `agent/sanitize.py` | HTML→text, prompt-injection screening, outbound secret scrub |
| `agent/memory.py` | SQLite: seen-set, two-phase intents, post ledger, runs, breaker |
| `agent/decide.py` | Scoring, the do-nothing decision, reply/thread composition |
| `agent/runner.py` | One cycle: control line, reconciliation, verified write |
| `agent/cli.py` | `bootstrap` / `run` / `status` / `evidence` / `reset-breaker` |

### Scheduler

`cron` runs `scripts/run_cycle.sh` every three hours at :07. The script loads
`.env`, refuses to start without a token, and holds a PID-file lock so a slow
cycle can never overlap the next one and double-post. It exits 0 on handled
failures so cron does not treat the agent as a crash loop. Each invocation is a
complete, independent cycle — no daemon, no human input, nothing to press.

### Canvas access

Read: the discussion topic, its entries, and their replies, with `Link`-header
pagination. Write: only `create_entry` and `create_reply`. There is deliberately
**no method that can edit or delete** anything, so no instruction — from a forum
post or anywhere else — can make the agent modify another person's contribution.

Before every write the agent re-fetches the discussion topic and parses the
first line of its description for `COURSE-TEAM CONTROL:`. It **fails closed**:
`RUNNING` is the only value that permits posting. A missing line, an
unrecognised value, a control line that is not at the very top, or an empty
description are all treated as `PAUSED`.

### Decision logic

Gates, in order: breaker closed → under the hourly cap → control line RUNNING →
something worth saying. Entries are scored on substance (length), novelty
(unseen), how under-served the thread is, whether it asks a question, and
whether it touches topics the agent can speak to concretely. It replies to the
single highest-scoring entry, and skips anything that is its own, already
replied to, under 120 characters, or quarantined as an injection attempt.

If nothing merits a reply it may open **one** new thread, but only when the
forum already contains substantive discussion, no post has gone out in six
hours, and at least 24 hours have passed since its last thread. Otherwise it
posts nothing and records `no_action` — the common case, by design.

Reply text is written by the local `hermes` CLI with forum content passed inside
an untrusted-data fence; a deterministic template is used if no model is
reachable, so the agent still works headless.

### Persistent local memory

SQLite at `var/memory.db` (WAL, `synchronous=FULL`), five tables:

- `seen_items` — every entry/reply id it has read, with `is_self` so it never
  responds to its own posts
- `intents` — the two-phase write log (below)
- `posts` — durable ledger of confirmed posts; backs rate limiting and evidence
- `runs` — one row per cycle with outcome and reason: the autonomy audit trail
- `breaker` — consecutive failures and cooldown deadline

### Idempotency and the lost-acknowledgement case

The hard case is not a retry — it is a write that Canvas **commits** and the
agent never hears about, because the process died or the connection dropped
after the POST. Three mechanisms, in layers:

1. **Writes are at-most-once at the HTTP layer.** POSTs are never blindly
   retried, because a network error after a POST may mean the entry was already
   saved. Only `429` (rejected outright, nothing written) is retried on a write.
   GETs retry freely with exponential backoff plus jitter.
2. **A two-phase intent log.** Before any POST the agent commits an `intents`
   row keyed by `sha256(topic | action | parent | normalised-body)`. The post is
   confirmed only once Canvas returns an id.
3. **Reconciliation on the next cycle.** A pending intent means an unknown
   outcome. The next cycle re-reads the thread and matches its own posts by
   normalised body hash. Found ⇒ confirm locally, **no second post**. Not found
   ⇒ abandon the intent so the normal path may try again.

Because the rate-limit ledger lives in the same database, the three-per-hour
ceiling also survives restarts — an in-memory counter would reset and quietly
breach it.

### Verification

After every successful write the agent re-reads the thread and confirms its new
entry id is present before marking the post `verified=1`. Unverified posts are
visible in `python3 -m agent status`.

### Rate limits and stopping rule

- At most **3 posts/hour** (hard-capped in config), enforced from the durable ledger
- At most 1 post per cycle; **≥45 min between posts** even when under the hourly cap
- New threads at most once per 24h
- Read retries: 4 attempts, exponential backoff with jitter, honours `Retry-After`
- **Circuit breaker**: after 5 consecutive failed cycles it opens for 3 hours and
  subsequent cycles exit immediately as `blocked`. A successful cycle resets it.
  Clear manually with `reset-breaker`.

## Safety

| Concern | Control |
|---|---|
| Credentials | Env/`.env` only (gitignored, `chmod 600`); sent as a Bearer header; never in argv, URLs, or the database |
| Log leakage | Every log record passes a redacting filter that scrubs token-shaped strings, `Authorization:` headers, and `access_token=` params |
| Outbound leakage | `scrub_outbound()` strips token-shaped text and invisible characters from anything about to be posted |
| Prompt injection | Forum text is flattened, stripped of zero-width/bidi characters, screened against 14 patterns (override, exfiltration, command, destructive, puppet), and quarantined on a high-severity hit — never obeyed, and the agent won't even reply to it. Content reaches the model only inside an explicit untrusted-data fence |
| Blast radius | Stdlib only; one API host; no shell execution; no file writes outside `var/`; no Canvas update/delete capability at all |
| Pause compliance | Control line re-read before every write, failing closed |
| Other participants | No code path can edit or delete another person's post |

## Testing

```bash
python3 tests/test_agent.py       # 29 tests, offline, no token needed
python3 scripts/failure_demo.py   # failure-injection evidence → var/failure_demo.md
```

Tests run against an in-memory fake Canvas with scriptable faults (HTTP 500/503,
401, timeouts, malformed JSON, swallowed acknowledgements), covering the control
line failing closed, injection quarantine, idempotency, duplicate events,
restart persistence, the rate-limit cap, and the breaker opening and resetting.

## Repository layout

```
agent/        config, canvas client, sanitizer, memory, decision logic, runner, CLI
scripts/      run_cycle.sh (cron entry), install_cron.sh, failure_demo.py
tests/        offline test suite with a fake Canvas
docs/         ARCHITECTURE.md, SUBMISSION.md
var/          local state — gitignored (memory.db, logs, evidence)
.env.example  configuration template; real .env is never committed
```
