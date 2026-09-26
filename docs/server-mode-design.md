# Server mode: a scoped design, phase 1 built

This describes a piece of the roadmap (`adapters/bitbucket_dc` + `deploy/`).
Phase 1 below — ingest only — is built, tested against a real Postgres, and
covered by CI. Phases 2 and 3 are not: they remain what would need to be
true before they exist, not a promise of order. Treat everything past
"A path that ships value before the hard part" as an RFC, and that section
itself as the line between what's real and what's still proposed;
[design.md](design.md) is the status report for the rest of the system.

## Try it

```
docker compose -f deploy/docker-compose.yml up
```

starts Postgres and the ingest API on `localhost:8000` — `GET /healthz`,
`POST /reviews`. Point a CI adapter at it with `GROUNDTRUTH_INGEST_URL` (and
`GROUNDTRUTH_INGEST_TOKEN` if the server has one configured — see
`.env.example`) and its next run's JSON output lands in `reviews` and
`findings`. Nothing reads that data back out yet; see "What a dashboard and
a feedback loop are, concretely" below for what would.

## Two questions, one answer

Two unrelated gaps turn out to need the same piece of infrastructure:

1. **Bitbucket Data Center has no free per-pull-request CI container.**
   Every other platform gives you a runner that starts on a pull-request
   event and exits when the job ends — that runner *is* the trigger, the
   compute, and (via `fetch-depth: 0`) the diff source, for free. Bitbucket
   Data Center doesn't, so something has to sit somewhere and receive its
   webhooks instead. That something is a server.
2. **Nothing outlives one pull request.** The only memory this design has
   is a fingerprint marker inside the PR's own summary comment (see
   [design.md](design.md#what-it-deliberately-does-not-use)). Once a PR
   merges, that history is unindexed prose in a closed comment thread. There
   is no way to ask "what did we find last month," no signal from a
   dismissed finding or a 👎 reaction that could tune `min_confidence`, and
   no status endpoint for anyone who isn't looking at the PR itself.

Both are answered by the same optional, self-hosted component: a small
server with a database behind it. Neither answer changes anything about
the CLI or the three CI adapters that already work — this is additive, not
a rewrite. Someone who just wants a workflow file never needs to know this
exists.

## What does not change

- `groundtruth review` stays the one command every front door calls, and
  stays capable of running with nothing behind it but an API key.
- `adapters/github`, `adapters/gitlab`, `adapters/bitbucket_cloud` are
  unmodified. They already speak the platform's API and read the plain
  JSON `groundtruth review --format json` prints; that boundary is exactly
  what lets a fourth, heavier front door exist without touching the other
  three.
- `context_engine` and `quality_gate` still import nothing platform- or
  server-specific. The server is a new Trigger, a new Publish, and a new
  layer underneath — never a fork of the pipeline in between.

## Where a queue comes back

The original version of this system used Redis and `arq` to queue
reviews. That wasn't the wrong tool — it was answering a question this
design doesn't currently have. A CI runner is already a queue of one: the
platform starts a job, the job runs to completion or is superseded, and
there is nothing else competing for that runner's attention. A real,
long-running server answers to many repositories at once, so the question
comes back, and the same answer applies again:

- **Dedupe by PR, not by event.** Two webhooks for the same pull request
  (a second push arriving before the first review finished) should collapse
  to one in-flight job, newest-wins — the server-mode equivalent of the
  `concurrency: cancel-in-progress` block the CI workflows now carry (see
  the [FAQ](guide/faq.html#what-happens-if-i-push-twice-quickly)).
- **Retry, don't fail the webhook.** A transient LLM timeout should retry
  the job, not drop the review or block the HTTP response Bitbucket is
  waiting on.
- **Backpressure across tenants.** One busy repository's review queue
  should not starve another's.

`arq` over Redis is still a reasonable choice here — asyncio-native, small,
and it fits a small FastAPI-style receiver without pulling in a heavier
broker. This is the one part of the original design that server mode
brings back essentially unchanged; it just moves from "the whole
architecture" to "the queue behind one optional service."

## The database

Three tables cover what a dashboard or a feedback loop actually needs to
read:

- **`reviews`** — one row per run: repo, platform, PR/MR number, base and
  head SHA, model and verify\_model, timestamps, cost estimate, and whether
  it completed (`review_incomplete`, already a field the CLI's JSON output
  carries — this table is largely that JSON, stored instead of only
  printed).
- **`findings`** — one row per candidate the gate ever saw: fingerprint,
  file, line, category, severity, confidence, whether it posted, and if
  not, which stage dropped it and why. This is `cli.render_json`'s
  `findings` and `dropped` arrays, persisted instead of only printed —
  which is why `render_json` gained a full `dropped` array of its own
  (previously just a count) as part of building this.
- **`feedback`** — one row per signal on a posted finding: a reaction, a
  resolved thread, an edited line, where it came from, and when. This is
  the table that doesn't exist anywhere today, because nothing currently
  listens for it.

Postgres over SQLite: multiple webhook workers can write concurrently, and
a dashboard is exactly the kind of ad-hoc querying Postgres is built for.

## What a dashboard and a feedback loop are, concretely

Not a new frontend framework decision yet — that's premature before the
data exists. Concretely, in order of how little each one requires:

1. A read-only JSON API over `reviews` and `findings` — "how many findings
   this repo, this week, by stage" — is a handful of SQL views. This alone
   answers "check status" for anyone who isn't looking at the PR.
2. A feedback signal — a 👎 reaction, a resolved-without-comment thread, a
   dismissed suggestion — written to `feedback`, keyed to a fingerprint.
   Each platform's webhook shape differs here and is the least-certain,
   most-per-platform-effort part of this whole design.
3. Fine-tuning, in the narrow sense this system can actually support today:
   not retraining a model, but using `feedback` to suggest a per-repo
   `min_confidence` or `dimensions` change — "this repo's false-positive
   rate on `style` findings is high, consider raising the bar" — the same
   kind of number the eval harness already produces, aggregated over real
   traffic instead of a labeled suite.

## A path that ships value before the hard part

The Bitbucket Data Center webhook receiver is the part that requires the
most new code (a receiver, the queue, retry handling). The database and a
read-only dashboard do not depend on it existing first:

1. **Ingest only — built.** Each CI adapter POSTs its JSON output to a
   `/reviews` endpoint if `GROUNDTRUTH_INGEST_URL` is set
   (`adapters/common.py::maybe_ingest`); unset, it's the one `if` doing
   nothing, same as before this existed. `groundtruth.server` (an optional
   `pip install "groundtruth-review[server]"`) is the FastAPI app and the
   three-table schema (`src/groundtruth/server/`), tested against a real
   Postgres in CI (the `server-test` job) rather than a mock, since the
   thing worth proving here is the SQL. This alone unlocks history for
   GitHub, GitLab and Bitbucket Cloud users who stand up nothing but this
   ingest API and a database — no dashboard reads it yet (that's still
   below), but the data to build one on is there from the first ingested
   review.
2. **The Bitbucket Data Center receiver — not built.** Webhook in, `arq`
   job, same core pipeline, `reviews`/`findings` written directly instead
   of only POSTed. This is where the queue described above is load-bearing
   rather than optional.
3. **Feedback ingestion — not built.** Per-platform webhook subscriptions
   writing to `feedback`, and the first read query that turns it into a
   suggestion rather than just a number.

Phases 2 and 3 are where the actual uncertainty in this document lives, and
where a review of this RFC should focus before anyone starts phase 2.
