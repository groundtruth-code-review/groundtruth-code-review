# Contributing

Thanks for looking at this. It's early but runnable: the pipeline, the CLI,
the eval harness and three platform adapters exist. The self-hosted server
mode for Bitbucket Data Center does not, so there's real room to shape that
one.

## Setup

```bash
git clone https://github.com/groundtruth-code-review/groundtruth-code-review
cd groundtruth-code-review
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest             # the package
pytest adapters    # the adapters, which import no package code
ruff check .       # all three green before you start, and before you open a PR
```

No API key needed to develop on `context_engine` or `quality_gate` — neither
makes a network call in its test suite (the skeptic pass is tested against a
fake LLM implementing the same narrow `complete_json` protocol the real
client uses).

## Ground rules, carried forward on purpose

A few of these came from real failures in the internal system this project
is modeled on, not from taste — breaking them tends to reintroduce a bug
that already happened once:

- **A finding's identity never includes anything a model wrote** — only
  what it points at (file, changed-code content, category). See
  `quality_gate/fingerprint.py`'s docstring for why; there's a regression
  test (`test_two_distinct_findings_in_the_same_hunk_do_not_collapse`) that
  should never go red.
- **Skeptic confidence multiplies, never averages.** If a change makes
  `test_multiply_not_average_a_confident_wrong_answer_cannot_win` fail,
  that's the change to reconsider, not the test.
- **Nothing in `context_engine` or `quality_gate` imports anything
  platform-specific** (no GitHub/GitLab/Bitbucket API calls, no SCM-shaped
  data types). That boundary is what makes "any platform" true later —
  keep platform logic in `adapters/` once it exists.
- **A missing language grammar, an unparseable file, or a failed search
  degrades the review — it never crashes it.** Functions in `context_engine`
  return empty results on failure, not exceptions, on purpose. Match that
  pattern in new code.
- **Secrets are environment variables. Full stop.** Never add a code path
  that reads a key from a config file or accepts one as a plain CLI
  argument that'd show up in shell history.
- **The summary may only regroup findings that already passed the gate.** It
  is the one piece of text on a pull request the gate never checked itself,
  so `summary.py` throws away any summary naming a file outside the verified
  set and falls back to the plain list. Never relax that check to make the
  wording nicer.
- **Adapters import `common`, never `groundtruth`.** Each adapter owns its
  API calls and nothing else; shared wording lives in `adapters/common.py`.
  An adapter that imports the package would stop being installable with
  nothing but a Python interpreter.
- **A new gate stage records why it dropped a finding.** `DropStage` is what
  the eval harness grades against; a silent drop is invisible to it.

## What's most useful to work on right now

Check the [Roadmap](README.md#roadmap) in the README — anything unchecked is
open. `adapters/bitbucket_dc` plus `deploy/` is the remaining module, and the
honest version of it needs real repositories to profile: the claim that
module makes is a deployment sized from measurement, not a Helm chart full
of guessed numbers.

Adding a case to `cases/` is the smallest useful contribution, and often the
most valuable: every case is a bug the tool can never silently stop
catching.

## Pull requests

Small and focused beats large and sweeping. Add or update tests for
anything behavioral — this project measures itself (see the eval-harness
item on the roadmap once it exists), so untested behavior is the thing most
likely to regress silently.
