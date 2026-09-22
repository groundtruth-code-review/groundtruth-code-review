# groundtruth-review

**An AI code reviewer that proves what it claims.**

[![ci](https://github.com/varunkumar-dev/groundtruth-review/actions/workflows/ci.yml/badge.svg)](https://github.com/varunkumar-dev/groundtruth-review/actions/workflows/ci.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Nothing gets posted to a pull request unless it's grounded in code that
provably exists in the diff and survives a second, adversarial AI pass
cross-examining it. No vector database, no RAG — context comes from a
deterministic pipeline (git + Tree-sitter + a caller search), so a wrong
retrieved snippet can't produce a confident, wrong finding.

Bring your own model and your own API key — Anthropic, OpenAI, Azure, Bedrock,
or a fully local Ollama model, picked by one config line. Runs anywhere your
code already lives: GitHub, GitLab, Bitbucket Cloud, or self-hosted Bitbucket
Data Center.

> **Status: early / alpha.** The pipeline, the CLI, the eval harness and the
> GitHub, GitLab and Bitbucket Cloud adapters are implemented and tested
> (186 tests, all green). A self-hosted server mode for Bitbucket Data
> Center is the remaining piece; see [Roadmap](#roadmap). Nothing here is
> published to PyPI yet — the install instructions below use a git URL until
> it is.

## How a review flows

Seven stages, each handing the next exactly one thing. The dotted lines are
the only three model calls, and each is a round trip: stage 4 sends one file's
diff with the assembled context and gets candidate findings back, stage 5
sends a single finding with its evidence and gets a verdict, stage 6 sends the
verified set and gets one grouped summary. Everything else — parsing,
searching, quote matching, fingerprinting — is ordinary code.

```mermaid
flowchart TD
    A["1 Trigger<br/>CI on a pull request"] --> B["2 Collect<br/>diff + file versions"]
    B --> C["3 Build context<br/>AST parse + caller search"]
    C --> D["4 Propose<br/>one call per changed file"]
    D --> E["5 Verify<br/>five checks"]
    E --> F["6 Summarize<br/>one call per pull request"]
    F --> G["7 Publish<br/>PR comments"]

    M(["Model via LiteLLM<br/>your key, any provider"])
    D <-.-> M
    E <-.-> M
    F <-.-> M

    E -.-> X["dropped<br/>no quote, off-diff line,<br/>duplicate, low confidence"]
```

The same pipeline, with a worked example traced through every stage and the
handoffs animated, is at
**[varunkumar-dev.github.io/groundtruth-review](https://varunkumar-dev.github.io/groundtruth-review/)**.

Stage 3 is where the interesting work happens, and it calls no model at all:
a changed line becomes the whole function that contains it, that function's
real callers are found by text search, and a changed signature promotes those
callers to must-include &mdash; which is how a break in a file the pull request
never touched still reaches the reviewer.

## Why

Most AI PR reviewers optimize for *coverage* — say something about everything.
This one optimizes for *precision* — a quiet, accurate bot beats a chatty,
clever one, because a wrong comment costs more trust than a missed one. Two
decisions follow from that:

1. **Context is deterministic, not retrieved.** Instead of embeddings and
   similarity search, the reviewer parses the actual changed function, finds
   its real callers with a text search, and — if a function's signature
   changed — promotes those callers to *must-include* context. A caller still
   passing the old argument list is exactly the bug a diff-only review can't
   see.
2. **Nothing posts unverified.** Every finding must quote code that literally
   exists in the diff (a hallucination check), get cross-examined by a second
   LLM call whose confidence is *multiplied* against the first — never
   averaged, because averaging lets a confident wrong answer outvote a
   skeptical right one — and clear a fingerprint check so re-runs never
   duplicate a comment or falsely mark a live bug resolved.

## How it's put together

```
src/groundtruth/
├── context_engine/   # diff → hunks → enclosing functions → callers →
│                      # signature-change detection → a budgeted context payload
├── quality_gate/      # hallucination check, adversarial cross-examination,
│                      # fact-based fingerprinting — the verification pipeline
├── llm/                # a thin, embedded LiteLLM wrapper: model choice + BYOK
│                       # live here, not in a separately-deployed proxy
├── reviewer.py          # PROPOSES findings — the least-trusted module;
│                        # quality_gate does the trusting. Reviews one file
│                        # at a time (not the whole PR in one call) — a
│                        # single large-diff pass measurably loses recall
├── summary.py            # one cheap call that groups verified findings into
│                         # a summary comment — and throws its own output away
│                         # if it names anything outside the verified set
├── eval/                  # replay labeled cases, grade caught / gated /
│                          # missed / false positive — `groundtruth eval`
├── git_source.py           # the only place that shells out to git
├── config.py                # .groundtruth.yml loading (and refusing to load a
│                            # config that looks like it holds a live key)
└── cli.py                    # `groundtruth review` and `groundtruth eval` —
                               # the commands every front door below calls

adapters/
├── common.py          # what every adapter says: summary markdown, inline
│                       # comment bodies, and the hidden fingerprint marker
│                       # that carries the review's memory to the next push
├── github/             # a composite GitHub Action (action.yml + publish.py)
├── gitlab/              # a GitLab CI job (notes + positioned discussions)
└── bitbucket_cloud/      # a Pipelines step (comments with inline anchors)
```

Each adapter holds only its own API. `adapters/common.py` holds the text, so
a wording fix reaches all three at once, and no adapter imports
`groundtruth` itself — they run the CLI and read its JSON.

Neither `context_engine` nor `quality_gate` imports anything platform-specific
— they take a diff and a repo path in, and return findings out. Every SCM and
every trigger (a CI job, a webhook server) is a thin adapter around that same
core, which is what makes "any platform, any model" a real property of the
design instead of a slogan. Three adapters are the proof rather than the
promise: each is a few dozen lines of API calls over the same JSON.

## Using it on GitHub

Add this workflow to any repo (`fetch-depth: 0` matters — the CLI needs full
history to diff against the base ref, not the default shallow clone):

```yaml
# .github/workflows/groundtruth.yml
on: pull_request
permissions:
  pull-requests: write
jobs:
  review:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with: { fetch-depth: 0 }
      - uses: varunkumar-dev/groundtruth-review/adapters/github@main
        with: { model: anthropic/claude-sonnet-5 }
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
```

That's the whole install — one repo secret, one 12-line workflow file. No
server, no database, no webhook to register.

## Using it on GitLab

Copy [`adapters/gitlab/gitlab-ci.example.yml`](adapters/gitlab/gitlab-ci.example.yml)
into your `.gitlab-ci.yml`. Two settings matter: `GIT_DEPTH: 0`, because the
review diffs against the merge target a shallow clone does not contain, and a
project access token with `api` scope — `CI_JOB_TOKEN` cannot post notes.
Findings land as positioned discussions, the summary as one edited note.

## Using it on Bitbucket Cloud

Copy [`adapters/bitbucket_cloud/bitbucket-pipelines.example.yml`](adapters/bitbucket_cloud/bitbucket-pipelines.example.yml)
into your `bitbucket-pipelines.yml`. It needs `clone: depth: full` and a
fetch of the destination branch, which Pipelines gives you by name rather
than as a SHA. Findings land as inline-anchored comments.

## Running it in Kubernetes, or any container

There is no server to deploy and no Helm chart to install: a review is one
command that reads a diff and exits. What in-cluster CI needs is the image,
so a Tekton task, an Argo Workflows step or a Jenkins-on-Kubernetes agent can
run the review as a pod:

```yaml
image: ghcr.io/varunkumar-dev/groundtruth-review:0.1.0
args: ["review", "--repo", "/workspace", "--base", "origin/main", "--format", "json"]
env:
  - name: ANTHROPIC_API_KEY
    valueFrom:
      secretKeyRef: { name: groundtruth, key: api-key }
```

The image carries `git` and `ripgrep`, runs as an unprivileged user, and its
`Dockerfile` is multi-stage: the runtime stage installs a wheel that only
exists if lint, both test suites and the eval thresholds passed first, so a
failing suite cannot produce a shippable image.

Inject the provider key from a real secrets manager — the `secretKeyRef`
above is the minimum, and External Secrets or a Vault agent is better. The
key is never read from config, only from the environment.

## Quickstart (CLI + library)

```bash
git clone https://github.com/varunkumar-dev/groundtruth-review
cd groundtruth-review
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

The CLI works against any local git repo — no API key needed to see the
shape of the output, since `--dry-run` never calls the model:

```bash
groundtruth review --repo /path/to/some/repo --base main --head HEAD --dry-run --format text
```

With a real key set (`ANTHROPIC_API_KEY`, matching whatever `model:` your
`.groundtruth.yml` names), drop `--dry-run` to get real, verified findings:

```bash
groundtruth review --repo /path/to/some/repo --base main --head HEAD --format json
```

This example is copy-pasteable and runs as-is (no repo, no API key needed) —
it shows the context engine finding both the changed function *and* the
caller elsewhere in the tree that a diff-only review would never see:

```python
import tempfile
from pathlib import Path
from groundtruth.context_engine import build_context, parse_diff

diff_text = """diff --git a/invoice.py b/invoice.py
--- a/invoice.py
+++ b/invoice.py
@@ -1,2 +1,3 @@
 def calculate_discount(price):
-    return price * 0.9
+    rate = 0.9
+    return price * rate
"""
head_source = "def calculate_discount(price):\n    rate = 0.9\n    return price * rate\n"

with tempfile.TemporaryDirectory() as repo:
    Path(repo, "invoice.py").write_text(head_source)
    Path(repo, "checkout.py").write_text(
        "from invoice import calculate_discount\n\n"
        "def checkout(p):\n    return calculate_discount(p)\n"
    )

    ctx = build_context(
        repo_root=repo,
        diffmap=parse_diff(diff_text),
        head_sources={"invoice.py": head_source},
    )
    for block in ctx.blocks:
        print(block.label)
        print(block.text)
        print("---")

# invoice.py#L1-3 (changed)
# def calculate_discount(price):
#     rate = 0.9
#     return price * rate
# ---
# checkout.py#L3-4 (caller of calculate_discount)
# def checkout(p):
#     return calculate_discount(p)
# ---
```

## Configuration

```yaml
# .groundtruth.yml — safe to commit: it can never hold a key
model: anthropic/claude-sonnet-5   # or openai/gpt-4o, ollama/qwen2.5-coder, ...
max_cost_per_run: 1.00             # personal-use safety ceiling, whole run
min_confidence: 0.7                # the quality gate's default bar
max_inline_comments: 10
context_token_budget: 25000        # context assembled per review
max_diff_tokens_per_call: 6000     # a bigger file is reviewed in hunk groups
summary: true                      # one cheap call to group the findings
dimensions: [correctness, security, conventions]

# Optional: a cheaper model for the checking stages. Unset means every stage
# uses `model` above — splitting the tiers is something you opt into.
# verify_model: anthropic/claude-haiku-4-5-20251001
# summary_model: anthropic/claude-haiku-4-5-20251001

# llm_base_url: https://litellm.your-org.internal   # org mode — see docs
```

`max_cost_per_run` covers the whole run, not just the review pass: the
ceiling is re-checked before the verification pass (one call per candidate)
and before the summary call, because the number of those calls is not known
until the review returns.

`config.py` refuses to load a `.groundtruth.yml` that contains anything
key-shaped (a `sk-...` prefix, or a long opaque token) — loudly, with an
error naming the offending field, not a silent skip. That's what makes the
"safe to commit" claim above enforced rather than just asked nicely.

API keys are **environment variables, always** — `ANTHROPIC_API_KEY`,
`OPENAI_API_KEY`, etc. — never config, never committed. See `.env.example`.
For a team or CI deployment, inject that environment variable from a real
secrets manager (HashiCorp Vault or a cloud equivalent) rather than a `.env`
file at all.

## Roadmap

- [x] `context_engine` — diff parsing, Tree-sitter chunking, caller search,
      signature-change detection, budgeted assembly
- [x] `quality_gate` — hallucination check, adversarial skeptic pass
      (multiply-not-average confidence), fact-based fingerprinting
- [x] `llm` — embedded LiteLLM wrapper: BYOK, any model by name, pre-call
      cost estimation
- [x] `reviewer` — the LLM pass that proposes candidate findings for
      `quality_gate` to verify or reject. Batched per changed file, not one
      call over the whole diff: a single pass over a large, multi-file PR
      measurably loses recall as the diff grows (a well-documented LLM
      behavior, not a context-window limit) — it reports the most-salient
      few issues and stops, then "finds more" on the next re-review once the
      diff has shrunk from earlier fixes. Reviewing file-by-file keeps every
      file's review call bounded regardless of overall PR size, so file 15
      gets the same attention as file 1
- [x] `git_source` + `config` — git integration and `.groundtruth.yml`
      loading, including the "refuses to load a config with a key in it" check
- [x] `cli` — `groundtruth review`, the one command every front door calls:
      `.groundtruth.yml` loading, `--model` override, `--dry-run` /
      `max_cost_per_run` safety ceiling, JSON and text output
- [x] `adapters/github` — a composite GitHub Action: upserted summary
      comment plus one inline comment per finding, stdlib-only, no
      dependency on the `groundtruth` package itself
- [x] `summary` — one cheap call that groups verified findings, gated by its
      own grounding check and falling back to the plain list on any failure
- [x] fingerprint memory between pushes — carried in a hidden marker inside
      the summary comment, so a re-review never repeats a comment and no
      database is needed
- [x] `eval` — labeled replay, catch / gated / missed / false-positive
      grading, and `--min-catch` / `--max-fp` thresholds for CI
- [x] `adapters/gitlab`, `adapters/bitbucket_cloud` — same core command,
      each speaking only its own API
- [x] a container image and the CI that gates it — multi-stage build whose
      test stage gates the wheel, published to GHCR on a version tag
- [ ] publish to PyPI, so installing stops meaning a git URL
- [ ] `adapters/bitbucket_dc` + `deploy/` — self-hosted server mode for orgs,
      and the Helm chart that installs it. Deliberately not started before
      the server exists: a chart with no workload to run, and sizing numbers
      nobody measured, would be YAML pretending to be a deployment
      (Docker Compose + Helm, not Kustomize — see the top-level design doc's
      reasoning)

## Measuring it

A review tool you cannot measure is one nobody can improve. `cases/` holds
labeled diffs with recorded model responses, so the suite runs offline and
free:

```bash
groundtruth eval --cases cases --min-catch 0.8 --max-fp 0.2
```

Every expected finding lands in one of three buckets, and the distinction
is the point: **caught** posted, **gated** proposed but rejected by a gate
stage, **missed** never proposed at all. Gated and missed both look like
silence and have opposite fixes — a stricter gate versus a weaker prompt or
thinner context. Posted findings matching nothing expected are false
positives. Both thresholds exit non-zero, so CI can gate a prompt or model
change on the bug catch rate instead of on a reading of the diff.

## Development

```bash
pip install -e ".[dev]"
pytest                     # the package: 148 tests
pytest adapters            # the adapters, which import no package code: 38
pytest --cov=groundtruth   # with coverage
ruff check .
```

## License

MIT — see [LICENSE](LICENSE).
