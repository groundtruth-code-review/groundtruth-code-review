#!/usr/bin/env python3
"""Generate the guide pages from one shell, so seven pages cannot drift apart.

Run from docs/:  python3 build_guide.py

Every page shares the sidebar, the head and the footer defined here. Page
bodies live in `PAGES` below, written as plain HTML fragments -- no template
language, because a docs site this size does not need one and a dependency
here would have to be installed before anyone could fix a typo.
"""

from pathlib import Path

REPO = "https://github.com/groundtruth-code-review/groundtruth-code-review"

NAV = [
    ("getting-started", "Getting started"),
    ("configuration", "Configuration"),
    ("integrations", "Integrations"),
    ("what-gets-sent", "What gets sent"),
    ("languages", "Language support"),
    ("troubleshooting", "Troubleshooting"),
    ("faq", "FAQ"),
]

SHELL = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{title} &middot; Groundtruth</title>
<meta name="description" content="{description}">
<meta name="color-scheme" content="light dark">
<meta property="og:title" content="{title} &middot; Groundtruth">
<meta property="og:description" content="{description}">
<meta property="og:type" content="article">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Ccircle cx='7' cy='7' r='4.6' fill='none' stroke='%230c6b64' stroke-width='1.8'/%3E%3Cpath d='M10.4 10.4 L14 14' stroke='%230c6b64' stroke-width='1.8' stroke-linecap='round'/%3E%3C/svg%3E">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wght@500;600;700&family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
<link rel="stylesheet" href="../assets/site.css">
</head>
<body>

<nav class="topbar">
  <div class="topbar-inner">
    <span class="brand"><a href="../index.html" style="color:inherit;text-decoration:none">Groundtruth</a></span>
    <span class="topnav">
      <a href="../index.html#why">Why</a>
      <a href="../index.html#stages">How it works</a>
      <a href="getting-started.html">Docs</a>
      <a href="{repo}">GitHub</a>
    </span>
  </div>
</nav>

<div class="wrap">
  <div class="doc-layout">
    <aside class="sidebar">
      <h5>Guide</h5>
      <ul>
{sidebar}
      </ul>
    </aside>

    <main class="doc-body prose">
{body}
      <div class="doc-nextprev">
        <span>{prev}</span>
        <span>{next}</span>
      </div>
    </main>
  </div>
</div>

<footer style="max-width:1140px;margin:0 auto;padding:40px 20px 0;border-top:1px solid var(--hair);font-size:15px;color:var(--muted)">
  <p style="max-width:68ch">
    Source and issues:
    <a href="{repo}" style="color:var(--accent)">github.com/groundtruth-code-review/groundtruth-code-review</a>.
    MIT licensed. This guide is generated from <code>docs/build_guide.py</code>.
  </p>
</footer>

</body>
</html>
"""

PAGES = {}

# --------------------------------------------------------------- getting started
PAGES["getting-started"] = (
    "How to install Groundtruth on GitHub, GitLab, Bitbucket Cloud or any container CI, and run your first review.",
    """
      <h1>Getting started</h1>
      <p class="doc-lede">A review is one command that reads a diff and exits. There is no server to run, no database to provision and no webhook to register &mdash; so installing it means adding a job to CI you already have.</p>

      <h2>What you need</h2>
      <ul>
        <li><b>Python 3.10 or newer</b>, if you are running the CLI directly. The GitHub Action and the container image bring their own.</li>
        <li><b>A full clone.</b> The review diffs against the base branch, which a shallow clone does not contain. Every integration page says how to turn shallow clones off.</li>
        <li><b>An API key for one provider</b> &mdash; Anthropic, OpenAI, Azure, Bedrock &mdash; or a local Ollama model, which needs no key and costs nothing.</li>
        <li><b>ripgrep</b> is optional. It makes the caller search much faster; without it a pure-Python walk does the same work with the same results.</li>
      </ul>

      <h2>Try it first without a key</h2>
      <p>Before wiring anything into CI, see what the pipeline assembles. <code>--dry-run</code> stops before the first model call, so it needs no key and costs nothing:</p>
      <div class="code-box">
        <pre><code>pip install "git+REPOURL.git"

groundtruth review \\
  --repo /path/to/your/repo \\
  --base main \\
  --head HEAD \\
  --dry-run --format text</code></pre>
      </div>
      <p>You get the cost estimate for the review pass and the list of context that would have been sent. If that looks wrong &mdash; wrong files, missing callers &mdash; fix it here, before spending anything.</p>

      <h2>Add it to GitHub</h2>
      <p>One workflow file and one repository secret:</p>
      <div class="code-box">
        <pre><code># .github/workflows/groundtruth.yml
on: pull_request
permissions:
  pull-requests: write
jobs:
  review:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
        with: { fetch-depth: 0 }
      - uses: groundtruth-code-review/groundtruth-code-review/adapters/github@main
        with: { model: anthropic/claude-sonnet-5 }
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}</code></pre>
      </div>
      <p>Add <code>ANTHROPIC_API_KEY</code> under <b>Settings &rarr; Secrets and variables &rarr; Actions</b>, and that is the whole install. <a href="integrations.html">GitLab, Bitbucket Cloud and Kubernetes</a> take about the same.</p>

      <div class="callout ok"><b>Two settings people miss.</b> <code>fetch-depth: 0</code> gives the job the history it needs to diff against the base ref. <code>pull-requests: write</code> lets it post; without it the review runs and then fails at the last step.</div>

      <h2>What happens on the pull request</h2>
      <p>Two things, and only ever these two:</p>
      <ul>
        <li><b>One summary comment</b>, created on the first run and edited in place on every push after. It never stacks up.</li>
        <li><b>One inline comment per finding</b>, on the line the finding quotes, carrying the severity, the category, the confidence and the quoted line.</li>
      </ul>
      <p>If nothing clears the quality gate, you get the summary comment saying so and no inline comments at all. That is the normal, intended outcome on a clean diff.</p>

      <h2>Next</h2>
      <ul>
        <li><a href="configuration.html">Configuration</a> &mdash; every setting, and how to make it quieter or louder.</li>
        <li><a href="what-gets-sent.html">What gets sent to the model</a> &mdash; the answer your security review will ask for.</li>
        <li><a href="languages.html">Language support</a> &mdash; what works fully, and what degrades.</li>
      </ul>
""",
)

# --------------------------------------------------------------- configuration
PAGES["configuration"] = (
    "Every .groundtruth.yml setting, what it does, and how to tune the reviewer quieter or louder.",
    """
      <h1>Configuration</h1>
      <p class="doc-lede">All configuration lives in one <code>.groundtruth.yml</code> at the root of the repository being reviewed. Every setting has a working default, so the file is optional.</p>

      <h2>The file is safe to commit</h2>
      <p>Not as a convention &mdash; as an enforced rule. <code>config.py</code> refuses to load a config containing anything key-shaped (an <code>sk-</code> prefix, or a long opaque token) and fails loudly, naming the offending field. A file that structurally cannot hold a secret is one nobody has to remember not to commit.</p>
      <p>Keys come from environment variables only: <code>ANTHROPIC_API_KEY</code>, <code>OPENAI_API_KEY</code> and so on, matching whichever provider your <code>model</code> names.</p>

      <h2>A complete file</h2>
      <div class="code-box">
        <pre><code># .groundtruth.yml
model: anthropic/claude-sonnet-5   # or openai/gpt-4o, ollama/qwen2.5-coder, ...
max_cost_per_run: 1.00             # whole-run ceiling, in USD
min_confidence: 0.7                # the bar a finding must clear to post
max_inline_comments: 10            # cap on comments per review
context_token_budget: 25000        # tokens of context assembled per review
max_diff_tokens_per_call: 6000     # a bigger file is reviewed in hunk groups
summary: true                      # one cheap call to group the findings
dimensions: [correctness, security, conventions]

# Optional: a cheaper model for the checking stages. Unset means every
# stage uses `model` above.
# verify_model: anthropic/claude-haiku-4-5-20251001
# summary_model: anthropic/claude-haiku-4-5-20251001

# Optional: sampling settings, per model. See "Sampling settings" below.
# model_params: {temperature: 1, top_p: 1, max_tokens: 16384}
# verify_model_params: {temperature: 0.1, max_tokens: 2048}

# Endpoints are NOT set here -- see "Endpoints" below.</code></pre>
      </div>

      <h2>Every setting</h2>
      <div class="tablewrap">
        <table class="compact">
          <thead><tr><th>Setting</th><th>Default</th><th>What it does</th></tr></thead>
          <tbody>
            <tr><td>model</td><td>anthropic/claude-sonnet-5</td><td>Any LiteLLM model string. The provider prefix decides which environment variable is read for the key.</td></tr>
            <tr><td>verify_model</td><td>same as model</td><td>Model for the skeptic pass. Can be a different provider from <code>model</code> &mdash; and there is a good reason for it to be.</td></tr>
            <tr><td>summary_model</td><td>same as verify_model</td><td>Model for the summary call.</td></tr>
            <tr><td>model_params</td><td>temperature 0.1, max_tokens 4096</td><td>Sampling settings for <code>model</code>: <code>temperature</code>, <code>top_p</code>, <code>max_tokens</code>.</td></tr>
            <tr><td>verify_model_params</td><td>follows its model</td><td>Settings for the verify model. Inherited from <code>model_params</code> only when the verifier runs the same model.</td></tr>
            <tr><td>summary_model_params</td><td>follows its model</td><td>Settings for the summary model, inherited the same way.</td></tr>
            <tr><td>max_cost_per_run</td><td>unset</td><td>Ceiling in USD for the whole run. Re-checked before the verification pass and again before the summary, because the number of those calls is not known until the review returns.</td></tr>
            <tr><td>min_confidence</td><td>0.7</td><td>Combined confidence (reviewer &times; skeptic) a finding must reach to post.</td></tr>
            <tr><td>max_inline_comments</td><td>10</td><td>How many findings can post. Survivors are ranked by severity &times; confidence and the rest are cut.</td></tr>
            <tr><td>context_token_budget</td><td>25000</td><td>Token ceiling for the assembled context. Optional blocks shrink to a signature line before being dropped, and every cut is reported.</td></tr>
            <tr><td>max_diff_tokens_per_call</td><td>6000</td><td>A file whose diff is bigger is reviewed in hunk groups rather than one oversized call. No hunk is ever skipped.</td></tr>
            <tr><td>summary</td><td>true</td><td>Whether to make the stage-6 call that groups findings into one sentence at the top of the comment.</td></tr>
            <tr><td>dimensions</td><td>correctness, security, conventions</td><td>What the review pass is told to look for.</td></tr>
          </tbody>
        </table>
      </div>

      <h2>Making it quieter</h2>
      <p>If the reviewer is saying too much, in the order worth trying:</p>
      <ul>
        <li><b>Raise <code>min_confidence</code></b> to 0.8 or 0.85. This is the strongest lever: confidence is the reviewer's own score multiplied by the skeptic's, so raising the bar cuts anything either model hedged on.</li>
        <li><b>Lower <code>max_inline_comments</code></b> to 3 or 5. Findings are ranked before the cut, so you keep the most severe and most confident ones.</li>
        <li><b>Narrow <code>dimensions</code></b> to <code>[correctness, security]</code>. Convention findings are the most subjective and the most numerous.</li>
      </ul>

      <h2>Making it catch more</h2>
      <p>If it is too quiet, first find out <i>why</i> &mdash; silence has two very different causes and they have opposite fixes. Run with <code>--format text</code> and read the drop line:</p>
      <div class="code-box">
        <pre><code>No findings survived the quality gate &mdash; 4 dropped (3 low confidence, 1 hallucination).</code></pre>
      </div>
      <p>Findings dropped at <b>low confidence</b> means the reviewer proposed them and the gate rejected them: lower <code>min_confidence</code>. Findings that were never proposed at all do not appear in that line: that is a context or prompt problem, and lowering the bar will not help. <a href="troubleshooting.html">Troubleshooting</a> walks through both.</p>

      <h2>Splitting the model tiers</h2>
      <p>Three stages call a model, and they do not need the same one. Stage 4 does the reasoning and sets the ceiling on review quality; stages 5 and 6 answer bounded questions on short prompts. A common setup:</p>
      <div class="code-box">
        <pre><code>model: anthropic/claude-sonnet-5
verify_model: anthropic/claude-haiku-4-5-20251001
summary_model: anthropic/claude-haiku-4-5-20251001</code></pre>
      </div>
      <p>Both tiers default to whatever <code>model</code> is set to, so splitting them is something you opt into rather than something that happens to you on an upgrade.</p>

      <h2>Verify with a different provider</h2>
      <p>The tiers do not have to come from the same company. Review with Claude and verify with GPT, or the other way round &mdash; each stage reads its own model string, and LiteLLM picks the provider and the key from its prefix:</p>
      <div class="code-box">
        <pre><code># Claude proposes, GPT cross-examines
model: anthropic/claude-sonnet-5
verify_model: openai/gpt-4o-mini

# or the other way round
model: openai/gpt-4o
verify_model: anthropic/claude-haiku-4-5-20251001</code></pre>
      </div>
      <p class="why"><b>Why this is worth doing:</b> the skeptic pass exists to be a second opinion, and a second opinion from the same model is not much of one. Two runs of one model share the same training, the same blind spots and the same habits of mind, and a model asked to judge its own kind of output is inclined to agree with it. A different provider's model was trained differently, so it is less likely to share the specific mistake the first one made. The verifier is never told which model proposed the finding, so the cross-examination is independent in fact, not just in name.</p>
      <div class="callout"><b>You need both keys.</b> Each provider reads its own environment variable, so a Claude-plus-GPT run needs <code>ANTHROPIC_API_KEY</code> <i>and</i> <code>OPENAI_API_KEY</code> set. Miss one and every call to that provider fails &mdash; which the review now reports as a failed run rather than a clean one.</div>
      <p>This is reasoning about why independence helps, not a measurement that it does on your code. <code>groundtruth eval --live</code> is how you find out &mdash; it runs the recall cases against your real models, and <code>--verify-model</code> lets you swap only the verifier between runs:</p>
      <div class="code-box">
        <pre><code># same provider for both
groundtruth eval --live --model anthropic/claude-sonnet-5 \
  --verify-model anthropic/claude-haiku-4-5-20251001

# cross-provider verifier, same cases, same reviewer
groundtruth eval --live --model anthropic/claude-sonnet-5 \
  --verify-model openai/gpt-4o-mini</code></pre>
      </div>
      <p>Compare the catch rate and the false positives. These are real, billed calls, and a handful of cases will not settle the question on their own &mdash; but it is a measurement rather than an argument.</p>

      <h2>Sampling settings</h2>
      <p>Each model can have its own <code>temperature</code>, <code>top_p</code> and <code>max_tokens</code>. Most setups never need them &mdash; the defaults are a low temperature, so the same diff gets the same review, and 4096 output tokens. The usual reason to change them is a <b>reasoning model</b>: many expect <code>temperature: 1</code>, and they spend part of <code>max_tokens</code> thinking before they answer, so a budget sized for a plain model can run out halfway through the JSON.</p>
      <div class="code-box">
        <pre><code>model: nvidia_nim/moonshotai/kimi-k3
model_params: {temperature: 1, top_p: 1, max_tokens: 16384}

verify_model: nvidia_nim/z-ai/glm-5.3-flash
verify_model_params: {temperature: 0.1, max_tokens: 2048}</code></pre>
      </div>
      <p>Settings belong to the model they were tuned for. A stage inherits them only when it runs the same model &mdash; so if the verifier names a different model, it does not pick up the reviewer's <code>temperature: 1</code>.</p>
      <ul>
        <li><b><code>stream</code> is not supported.</b> The review has to read the whole reply to check its JSON, so a streamed reply cannot be used, and in CI nobody is watching the tokens arrive. The file refuses to load if it is set.</li>
        <li><b>Only these three settings are accepted, within bounds</b> &mdash; <code>temperature</code> 0&ndash;2, <code>top_p</code> 0&ndash;1, <code>max_tokens</code> 1&ndash;65,536. This file can be edited by the pull request under review, so an open list would let it pass <code>api_base</code> off as a setting, and an unbounded <code>max_tokens</code> would let it make every call on your key enormous.</li>
      </ul>

      <h2>Endpoints</h2>
      <p>By default each model is called at its provider's own endpoint. To send a stage somewhere else &mdash; NVIDIA's API catalog, Azure, a model on your own GPUs, or your org's LiteLLM proxy &mdash; set its endpoint in the environment or on the command line:</p>
      <div class="tablewrap">
        <table class="compact">
          <thead><tr><th>Stage</th><th>Environment variable</th><th>Flag</th></tr></thead>
          <tbody>
            <tr><td>review (and default)</td><td>GROUNDTRUTH_BASE_URL</td><td>--base-url</td></tr>
            <tr><td>verify</td><td>GROUNDTRUTH_VERIFY_BASE_URL</td><td>--verify-base-url</td></tr>
            <tr><td>summary</td><td>GROUNDTRUTH_SUMMARY_BASE_URL</td><td>--summary-base-url</td></tr>
          </tbody>
        </table>
      </div>
      <p>Flags beat the environment. An endpoint follows the model it serves: when the summary inherits the verifier's model it inherits the verifier's endpoint too, so a model is never sent to another provider's server.</p>

      <h3>Example: NVIDIA's API catalog</h3>
      <div class="code-box">
        <pre><code>export NVIDIA_NIM_API_KEY=...
export GROUNDTRUTH_BASE_URL=https://integrate.api.nvidia.com/v1

groundtruth review --base main --model nvidia_nim/moonshotai/kimi-k3</code></pre>
      </div>
      <p>Use the model id shown on the model's page at build.nvidia.com, with <code>nvidia_nim/</code> in front of the <i>whole</i> id. NVIDIA's ids already carry their vendor &mdash; <code>openai/gpt-oss-20b</code>, <code>moonshotai/kimi-k3</code> &mdash; and LiteLLM reads the first segment as the provider, so <code>openai/gpt-oss-20b</code> on its own would be sent as an OpenAI call. The right name is <code>nvidia_nim/openai/gpt-oss-20b</code>, and Groundtruth refuses the other spelling before making any call. Note the key too: NVIDIA's endpoint reads <code>NVIDIA_NIM_API_KEY</code>, not <code>OPENAI_API_KEY</code>, even though it speaks the OpenAI protocol.</p>

      <h3>Example: Claude reviews, a model on NVIDIA verifies</h3>
      <div class="code-box">
        <pre><code>export ANTHROPIC_API_KEY=...
export NVIDIA_NIM_API_KEY=...

groundtruth review --base main \
  --model anthropic/claude-sonnet-5 \
  --verify-base-url https://integrate.api.nvidia.com/v1</code></pre>
      </div>
      <p>with <code>verify_model: nvidia_nim/&lt;model&gt;</code> in <code>.groundtruth.yml</code>. The review goes to Anthropic, the verification to NVIDIA, and the summary follows the verifier.</p>

      <div class="callout"><b>Why endpoints are never in <code>.groundtruth.yml</code>.</b> An endpoint decides which server receives your API key along with your code, and that file lives in the repository being reviewed &mdash; the pull request under review can edit it. Review bots are commonly run on <code>pull_request_target</code> so they can comment on forks, and that trigger gives the job your secrets. A fork that pointed the endpoint at its own server would collect your key on the first call. So the file refuses to load if it names an endpoint, and endpoints come from the same place keys do: whoever owns the key decides where it goes.</div>

      <h2>Flags that override the file</h2>
      <div class="tablewrap">
        <table class="compact">
          <thead><tr><th>Flag</th><th>Effect</th></tr></thead>
          <tbody>
            <tr><td>--model</td><td>Overrides <code>model</code> for this run.</td></tr>
            <tr><td>--dry-run</td><td>Estimate and stop. No model call, no key needed.</td></tr>
            <tr><td>--no-summary</td><td>Skip the stage-6 summary call even if the config enables it.</td></tr>
            <tr><td>--seen-fingerprint</td><td>A fingerprint already posted on this pull request; repeatable. Adapters pass these back automatically.</td></tr>
            <tr><td>--format</td><td><code>json</code> (what adapters read) or <code>text</code> (what humans read).</td></tr>
            <tr><td>--diff</td><td>Read the diff from a file or stdin instead of running git.</td></tr>
          </tbody>
        </table>
      </div>
""",
)

# --------------------------------------------------------------- integrations
PAGES["integrations"] = (
    "Install Groundtruth on GitHub, GitLab, Bitbucket Cloud, or any Kubernetes CI through the container image.",
    """
      <h1>Integrations</h1>
      <p class="doc-lede">Every integration runs the same command and reads the same JSON. What differs between them is how a diff arrives and where comments go &mdash; a few dozen lines of API calls each, nothing more.</p>

      <h2>GitHub</h2>
      <div class="code-box">
        <pre><code># .github/workflows/groundtruth.yml
on: pull_request
permissions:
  pull-requests: write
jobs:
  review:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
        with: { fetch-depth: 0 }
      - uses: groundtruth-code-review/groundtruth-code-review/adapters/github@main
        with: { model: anthropic/claude-sonnet-5 }
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}</code></pre>
      </div>
      <p>To use a different endpoint, set it in the workflow's <code>env</code> beside the key it goes with:</p>
      <div class="code-box">
        <pre><code>        env:
          NVIDIA_NIM_API_KEY: ${{ secrets.NVIDIA_NIM_API_KEY }}
          GROUNDTRUTH_BASE_URL: https://integrate.api.nvidia.com/v1</code></pre>
      </div>
      <p>To verify with a different provider, set <code>verify_model</code> in <code>.groundtruth.yml</code> and pass both keys:</p>
      <div class="code-box">
        <pre><code>        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}</code></pre>
      </div>
      <ul>
        <li><code>fetch-depth: 0</code> is required &mdash; the default shallow clone has no base commit to diff against.</li>
        <li><code>pull-requests: write</code> is required for the job's own <code>GITHUB_TOKEN</code> to post.</li>
        <li>Findings post as review comments on the line; the summary is an issue comment, edited in place on later pushes.</li>
      </ul>

      <h2>GitLab</h2>
      <p>Copy <code>adapters/gitlab/gitlab-ci.example.yml</code> into your <code>.gitlab-ci.yml</code>.</p>
      <div class="callout"><b>The job token will not work.</b> <code>CI_JOB_TOKEN</code> cannot post notes on a merge request. You need a project access token with the <code>api</code> scope, exposed to the job as <code>GROUNDTRUTH_GITLAB_TOKEN</code>. This is the single most common reason a GitLab setup runs clean and posts nothing.</div>
      <ul>
        <li>Set <code>GIT_DEPTH: 0</code> in the job, for the same reason GitHub needs <code>fetch-depth: 0</code>.</li>
        <li>Findings post as positioned discussions, which need both the base and head SHAs &mdash; the adapter reads them from <code>CI_MERGE_REQUEST_DIFF_BASE_SHA</code> and <code>CI_COMMIT_SHA</code>.</li>
        <li>The summary is a note, edited in place.</li>
      </ul>

      <h2>Bitbucket Cloud</h2>
      <p>Copy <code>adapters/bitbucket_cloud/bitbucket-pipelines.example.yml</code> into your <code>bitbucket-pipelines.yml</code>.</p>
      <ul>
        <li>Set <code>clone: depth: full</code>.</li>
        <li>Pipelines gives you the merge target as a <i>branch name</i>, not a SHA, so the step fetches <code>origin/$BITBUCKET_PR_DESTINATION_BRANCH</code> before reviewing.</li>
        <li>Authentication is an app password or repository access token over Basic auth, as <code>GROUNDTRUTH_BITBUCKET_USER</code> and <code>GROUNDTRUTH_BITBUCKET_APP_PASSWORD</code>.</li>
        <li>Findings post as inline-anchored comments; the summary is a plain comment on the pull request.</li>
      </ul>

      <h2>Kubernetes, Tekton, Argo, Jenkins</h2>
      <p>There is no Helm chart, because there is no workload to install &mdash; a review starts, reads a diff and exits. What in-cluster CI needs is the image:</p>
      <div class="code-box">
        <pre><code>image: ghcr.io/groundtruth-code-review/groundtruth-code-review:0.1.0
args: ["review", "--repo", "/workspace", "--base", "origin/main", "--format", "json"]
env:
  - name: ANTHROPIC_API_KEY
    valueFrom:
      secretKeyRef: { name: groundtruth, key: api-key }</code></pre>
      </div>
      <p>The image carries <code>git</code> and <code>ripgrep</code>, runs as an unprivileged user, and ships for both <code>amd64</code> and <code>arm64</code>. Its Dockerfile is multi-stage: the runtime stage installs a wheel that only exists if lint, both test suites and the eval thresholds passed first, so a failing suite cannot produce a shippable image.</p>

      <h2>What every adapter shares</h2>
      <ul>
        <li><b>One JSON contract.</b> Adapters read what <code>groundtruth review --format json</code> prints and nothing else. None of them import the package.</li>
        <li><b>One memory.</b> The fingerprints of everything already posted are carried in a hidden HTML comment inside the summary comment, so the next push does not repeat itself. No database, and it works anywhere comments can be edited.</li>
        <li><b>One failure mode.</b> If a single finding cannot be placed &mdash; a force-push race, a line the platform does not consider part of the diff &mdash; that comment is skipped with a warning and the rest of the review still posts.</li>
      </ul>
""",
)

# --------------------------------------------------------------- what gets sent
PAGES["what-gets-sent"] = (
    "Exactly what leaves your repository on each model call, where it goes, and what never leaves at all.",
    """
      <h1>What gets sent to the model</h1>
      <p class="doc-lede">This is the question a security review asks first, so here it is precisely, per call, from the code that makes them.</p>

      <h2>Three calls, and what each carries</h2>
      <div class="tablewrap">
        <table>
          <thead><tr><th>Call</th><th>What is in the prompt</th><th>How many</th></tr></thead>
          <tbody>
            <tr>
              <td>Stage 4 &mdash; propose</td>
              <td>One changed file's diff, plus the assembled context bundle: the enclosing functions of changed lines, and the callers of any changed function.</td>
              <td>One per changed file</td>
            </tr>
            <tr>
              <td>Stage 5 &mdash; verify</td>
              <td>One finding's title, category and quoted line, plus the evidence pool &mdash; the whole diff and every context block.</td>
              <td>One per candidate finding that passed the free checks</td>
            </tr>
            <tr>
              <td>Stage 6 &mdash; summarize</td>
              <td>The verified findings only: severity, file, line, category, title and quoted line for each. No diff, no context.</td>
              <td>One per pull request</td>
            </tr>
          </tbody>
        </table>
      </div>
      <p>Stages 1, 2, 3 and 7 make no model call at all. Parsing, the caller search, the quote match, the fingerprinting and the ranking all happen locally.</p>

      <h2>What never leaves</h2>
      <ul>
        <li><b>Files outside the diff and outside the context bundle.</b> The rest of the repository is never read into a prompt. A file is only included if it was changed, or if it contains a caller of a changed function.</li>
        <li><b>Your key, anywhere but the provider.</b> It is read from the environment and used to authenticate the call to the provider you chose. It is never written to a file, never logged, and the config loader refuses to start if it finds something key-shaped in <code>.groundtruth.yml</code>.</li>
        <li><b>To a server the reviewed code chooses.</b> The endpoint cannot be set from <code>.groundtruth.yml</code>, because that file can be edited by the pull request under review. Only the environment and the command line &mdash; the place your key lives &mdash; can say where requests go.</li>
        <li><b>Telemetry.</b> There is none. There is no service operated by this project for anything to be sent to &mdash; no accounts, no hosted component, no phone-home.</li>
      </ul>

      <div class="callout ok"><b>The cost estimate is local.</b> Token counting and pricing use LiteLLM's local tokenizer and its bundled price table. Estimating a review &mdash; including <code>--dry-run</code> &mdash; makes no network call, which is why a dry run works with no key at all.</div>

      <h2>Where it goes</h2>
      <ul>
        <li><b>Straight to your provider</b> by default. Anthropic, OpenAI, Azure, Bedrock &mdash; whichever your <code>model</code> names, called directly from the CI job with your key.</li>
        <li><b>To any endpoint you name</b> with <code>GROUNDTRUTH_BASE_URL</code> or <code>--base-url</code> &mdash; NVIDIA's API catalog, Azure, a model on your own hardware, or a self-hosted LiteLLM proxy. Point it at a proxy you run and nothing talks to a provider except infrastructure you control, which is also where org-wide spend caps and audit logs belong.</li>
        <li><b>Nowhere at all</b> if you point <code>model</code> at <code>ollama/&lt;model&gt;</code>. Every call goes to a local endpoint, no key exists, and no code leaves the machine running the review.</li>
      </ul>

      <h2>Retention</h2>
      <p>Groundtruth stores nothing. It is a process that starts, reads a diff and exits &mdash; no database, no cache, no artifacts beyond the comments it posts. The one piece of state it keeps between runs is the list of fingerprints in the summary comment on your own pull request, and a fingerprint is a SHA-1 of the file path, the surrounding code and the category &mdash; not the code itself.</p>
      <p>What your provider retains is your provider's policy, and worth reading. If that answer is unacceptable for your codebase, the local-model path exists precisely for that case.</p>
""",
)

# --------------------------------------------------------------- languages
PAGES["languages"] = (
    "Which languages Groundtruth parses, which ones get cross-file caller context, and what degrades when yours is not covered.",
    """
      <h1>Language support</h1>
      <p class="doc-lede">Two different mechanisms build the context, and they do not cover the same set of languages. The difference matters, so it is spelled out here rather than discovered.</p>

      <h2>The two mechanisms</h2>
      <div class="tablewrap">
        <table>
          <thead><tr><th>Mechanism</th><th>Coverage</th><th>What it gives you</th></tr></thead>
          <tbody>
            <tr>
              <td>Chunking (Tree-sitter)</td>
              <td>300+ grammars, detected from the file path</td>
              <td>A changed line expands to the whole function containing it</td>
            </tr>
            <tr>
              <td>Caller search</td>
              <td>18 file extensions, listed below</td>
              <td>The functions elsewhere in the repo that call a changed function</td>
            </tr>
          </tbody>
        </table>
      </div>

      <h2>Extensions the caller search covers</h2>
      <div class="code-box">
        <pre><code>.py   .js   .jsx  .ts   .tsx  .go
.java .rb   .rs   .c    .h    .cpp
.hpp  .cs   .php  .kt   .swift .scala</code></pre>
      </div>

      <div class="callout"><b>If your language is not in that list</b>, the reviewer still parses your changed functions and reviews them &mdash; but it will not find callers in other files, which means the cross-file contract break is exactly the class of bug it stops catching for you. That is the headline capability, so know it up front.</div>

      <h2>What degrades, and how</h2>
      <ul>
        <li><b>No grammar for the language:</b> chunking returns nothing for that file and the reviewer sees the raw diff without enclosing functions. The review still runs.</li>
        <li><b>A file that fails to parse:</b> same &mdash; that one file degrades, the rest of the review is unaffected. Nothing crashes over one bad file.</li>
        <li><b>Extension outside the caller list:</b> no caller context, so no signature-change promotion for that language.</li>
        <li><b>ripgrep missing:</b> no degradation at all. A pure-Python walk applies the same skip list, the same extension filter and the same size cap, and returns the same hits.</li>
      </ul>

      <h2>Directories never searched</h2>
      <p>Regardless of language, the caller search skips <code>.git</code>, <code>node_modules</code>, <code>vendor</code>, <code>.venv</code>, <code>venv</code>, <code>__pycache__</code>, <code>dist</code>, <code>build</code>, <code>.mypy_cache</code> and <code>.pytest_cache</code>, and ignores files over 2&nbsp;MB. Third-party code should not consume a review's context budget, and a finding raised against vendored code is a finding nobody in your repository can act on.</p>

      <h2>Adding a language</h2>
      <p>The caller list is one set in <code>src/groundtruth/context_engine/callers.py</code>. Adding an extension there is a one-line change, and both search paths read the same constant, so they cannot fall out of step. A pull request adding your language is welcome; a case in <code>cases/</code> exercising it is even more welcome.</p>
""",
)

# --------------------------------------------------------------- troubleshooting
PAGES["troubleshooting"] = (
    "Why a review posted nothing, why a comment failed to place, and every other failure mode with its actual cause.",
    """
      <h1>Troubleshooting</h1>
      <p class="doc-lede">Each of these is a real message from the code, with what actually causes it.</p>

      <h2>&ldquo;No findings survived the quality gate&rdquo;</h2>
      <p>Often correct &mdash; a clean diff should produce silence. To tell a clean diff from a misconfiguration, read the rest of that line:</p>
      <div class="code-box">
        <pre><code>No findings survived the quality gate &mdash; 4 dropped (3 low confidence, 1 hallucination).</code></pre>
      </div>
      <div class="tablewrap">
        <table class="compact">
          <thead><tr><th>Stage named</th><th>What happened</th><th>What to do</th></tr></thead>
          <tbody>
            <tr><td>(no drops at all)</td><td>The reviewer proposed nothing. Not a gate problem.</td><td>Check the context was assembled: re-run with <code>--dry-run --format text</code>. If the changed functions are missing, see <a href="languages.html">language support</a>.</td></tr>
            <tr><td>low confidence</td><td>Proposed, then scored below <code>min_confidence</code> once the skeptic's confidence was multiplied in.</td><td>Lower <code>min_confidence</code> toward 0.6 if you want more through.</td></tr>
            <tr><td>hallucination</td><td>The finding quoted code that is not in the diff or the context. It was dropped before any paid call.</td><td>Nothing &mdash; this is the gate doing its job. Frequent hits suggest a weaker model on stage 4.</td></tr>
            <tr><td>bad location</td><td>The quote was real, the line number was not inside a changed hunk.</td><td>Nothing. The comment could not have been placed anyway.</td></tr>
            <tr><td>dedupe</td><td>Already posted on this pull request in an earlier run.</td><td>Nothing. This is what stops repeat comments on every push.</td></tr>
          </tbody>
        </table>
      </div>

      <h2>&ldquo;no changes to review&rdquo;</h2>
      <p>The diff came back empty. Either the base ref is wrong, or the branch really has no changes against it. On CI, the usual cause is a shallow clone: the job has no base commit, so the diff is empty rather than an error.</p>

      <h2>&ldquo;git diff ... failed&rdquo;</h2>
      <p>A <code>GitError</code> carrying git's own message. Almost always a shallow clone. Set <code>fetch-depth: 0</code> on GitHub, <code>GIT_DEPTH: 0</code> on GitLab, or <code>clone: depth: full</code> on Bitbucket.</p>

      <h2>&ldquo;detected dubious ownership&rdquo;</h2>
      <p>Git refusing to operate on a checkout owned by a different user, which happens when a container runs as a non-root user against a mounted workspace. The published image already marks mounted paths as safe; if you built your own, add <code>git config --system --add safe.directory '*'</code> to it.</p>

      <h2>&ldquo;looks like it contains a live API key&rdquo;</h2>
      <p>A <code>ConfigError</code>, naming the field. Something key-shaped is in <code>.groundtruth.yml</code>. Move it to an environment variable &mdash; that check is what makes the config file safe to commit, so it fails loudly rather than skipping quietly.</p>

      <h2>&ldquo;would set where your API key is sent&rdquo;</h2>
      <p>A <code>ConfigError</code>: <code>.groundtruth.yml</code> names an endpoint (<code>llm_base_url</code>, <code>base_url</code>, <code>api_base</code>, or a per-stage one). Endpoints are refused in that file because the pull request under review can edit it. Move the value to <code>GROUNDTRUTH_BASE_URL</code> in the environment or <code>--base-url</code> on the command line. See <a href="configuration.html">configuration</a> for why.</p>

      <h2>Review calls fail with a reasoning model</h2>
      <p>The run reports failed calls, but the key and endpoint are right. Reasoning models spend part of <code>max_tokens</code> thinking, so the default 4096 can run out before the JSON is finished &mdash; and a reply cut off mid-object cannot be parsed. Raise it in <code>model_params</code>, and set <code>temperature: 1</code> if the model's own documentation asks for it. See <a href="configuration.html">sampling settings</a>.</p>

      <h2>&ldquo;is sent to NVIDIA's API catalog but is not prefixed 'nvidia_nim/'&rdquo;</h2>
      <p>The model name is missing its provider prefix. NVIDIA's ids include their vendor, so <code>openai/gpt-oss-20b</code> is read as provider <code>openai</code>, model <code>gpt-oss-20b</code> &mdash; the wrong provider, the wrong key, and a model NVIDIA does not serve. Put <code>nvidia_nim/</code> in front of the whole id: <code>nvidia_nim/openai/gpt-oss-20b</code>. The error names the corrected spelling.</p>

      <h2>Authentication fails against NVIDIA's endpoint</h2>
      <p>Two usual causes. The key variable is <code>NVIDIA_NIM_API_KEY</code>, not <code>OPENAI_API_KEY</code>, even though the endpoint speaks the OpenAI protocol. And the base URL must be exactly <code>https://integrate.api.nvidia.com/v1</code> &mdash; LiteLLM recognizes it by exact string. Groundtruth trims a trailing slash for you, but a different path will fall back to generic OpenAI handling and read the wrong key.</p>

      <h2>&ldquo;estimated cost ... exceeds max_cost_per_run&rdquo;</h2>
      <p>The run stopped before spending past your ceiling. The message names which phase it stopped at, because the ceiling is re-checked before the verification pass and again before the summary &mdash; the number of those calls is not knowable until the review returns. Raise the ceiling, shrink the diff, or inspect with <code>--dry-run</code>.</p>

      <h2>A finding posted, but no inline comment appeared</h2>
      <p>The adapter logs a warning and continues. Usually a force-push race: the head commit moved between the review and the post, so the platform no longer considers that line part of the diff. The summary comment still lists the finding. Re-running on the new head resolves it.</p>

      <h2>GitLab runs clean and posts nothing</h2>
      <p>Almost always the token. <code>CI_JOB_TOKEN</code> cannot post notes on a merge request. Use a project access token with the <code>api</code> scope as <code>GROUNDTRUTH_GITLAB_TOKEN</code>.</p>

      <h2>The same comment appeared twice</h2>
      <p>The review's memory lives in a hidden marker inside its own summary comment. If that comment is deleted, the memory goes with it and the next run has no way to know what it already said. Edit the summary comment freely &mdash; just do not delete it.</p>

      <h2>No caller context, ever</h2>
      <p>Check your file extension against the <a href="languages.html">18 the caller search covers</a>. Outside that list you get enclosing functions but no cross-file callers.</p>

      <h2>It costs more than expected on big pull requests</h2>
      <p>Stage 4 makes one call per changed file, so cost scales with files touched, not with repository size. A file whose diff exceeds <code>max_diff_tokens_per_call</code> is split into hunk groups, adding calls. Set <code>max_cost_per_run</code> to make that a hard stop rather than a surprise, and split the tiers so only stage 4 uses the expensive model.</p>
""",
)

# --------------------------------------------------------------- faq
PAGES["faq"] = (
    "Straight answers about monorepos, large pull requests, local models, data handling, and how mature this actually is.",
    """
      <h1>FAQ</h1>

      <h2>Does it block merges?</h2>
      <p>No. It posts comments. The CLI exits non-zero only on an actual error &mdash; a bad config, a git failure, a cost ceiling &mdash; not because it found something. If you want findings to block a merge, gate on the JSON output in a step of your own.</p>

      <h2>Will it work on a monorepo?</h2>
      <p>Yes, and the design assumes it. Review cost scales with the files a pull request changes, not with how large the repository is. The caller search walks the tree, which is where repository size shows up, and it skips vendor directories and anything over 2&nbsp;MB.</p>

      <h2>What happens on a 200-file pull request?</h2>
      <p>Stage 4 makes one call per changed file, so that is 200 review calls plus one skeptic call per surviving candidate. That is exactly the situation <code>max_cost_per_run</code> exists for. The reason it is not one giant call: a single pass over a huge diff reports a few obvious things and stops looking, so the fiftieth file would get no real attention.</p>

      <h2>Does it re-comment on every push?</h2>
      <p>No. Every finding gets a fingerprint built from the file, the surrounding code and the category &mdash; deliberately not the model's wording, so rephrasing the same finding does not make it look new. Fingerprints already posted are carried in the summary comment and passed back on the next run.</p>

      <h2>Can I run it with no API cost at all?</h2>
      <p>Yes. Set <code>model: ollama/&lt;model&gt;</code> and every call goes to a local endpoint. No key, no spend, and no code leaves the machine. Review quality then depends on the local model you run.</p>

      <h2>Does it store my code anywhere?</h2>
      <p>No. There is no database, no cache and no service operated by this project. See <a href="what-gets-sent.html">what gets sent to the model</a> for exactly what leaves the repository and where it goes.</p>

      <h2>How is this different from a linter?</h2>
      <p>A linter matches patterns it was taught in advance, which is why it is fast and deterministic and why it cannot notice that a function's new required argument broke a caller three files away. Groundtruth is built for that second kind of finding. They are complements &mdash; run both.</p>

      <h2>Why no vector database?</h2>
      <p>Because retrieval guesses. Similarity search returns code that <i>looks</i> related, and a wrong snippet produces a confident, wrong finding &mdash; the exact failure this tool is built to avoid. Parsing the actual function and searching for actual callers is exact, repeatable and free.</p>

      <h2>Is it deterministic?</h2>
      <p>The context is: the same diff assembles the same context every time. The model calls are not, which is the reason the verification stages are ordinary code rather than more model calls. The quote check, the location check, the fingerprint and the ranking give the same answer on every run.</p>

      <h2>Which model should I use?</h2>
      <p>Start with one strong model everywhere, then split the tiers once you see the bill: a strong model on stage 4 where the reasoning happens, a cheap one on stages 5 and 6. A finding that was never proposed cannot be recovered downstream, while a finding that was proposed still has to get past the gate.</p>

      <h2>Should the verifier be a different model from the reviewer?</h2>
      <p>If you can, yes &mdash; ideally from a different provider. Review with Claude and verify with GPT, or the reverse. The verifier's whole job is to doubt the first model, and a model is a poor judge of mistakes it would make itself. Nothing in the code prefers one arrangement; each stage takes its own model string. See <a href="configuration.html">verify with a different provider</a>, and note that it needs both providers' keys.</p>

      <h2>How mature is this?</h2>
      <p>Honestly: alpha. The pipeline, the CLI, the eval harness and three platform adapters are implemented and covered by 190 tests that run offline against fake model clients, plus labeled cases replayed on every build. What that does <i>not</i> prove is behaviour against a live model on your codebase at scale &mdash; no test can. Start it on one repository, read what it posts, and tune <code>min_confidence</code> before you turn it on everywhere.</p>

      <h2>What is still missing?</h2>
      <p>Publication to PyPI, so installing stops meaning a git URL; and a self-hosted server mode for Bitbucket Data Center, which is the one platform with no free per-pull-request CI container. The <a href="REPOURL#roadmap">roadmap</a> is kept current.</p>
""",
)


def sidebar_html(current: str) -> str:
    rows = []
    for slug, label in NAV:
        cls = ' class="current"' if slug == current else ""
        rows.append(f'        <li><a href="{slug}.html"{cls}>{label}</a></li>')
    return "\n".join(rows)


def main() -> None:
    out = Path("guide")
    out.mkdir(exist_ok=True)
    slugs = [s for s, _ in NAV]

    for index, (slug, label) in enumerate(NAV):
        description, body = PAGES[slug]
        prev_link = ""
        next_link = ""
        if index > 0:
            p_slug, p_label = NAV[index - 1]
            prev_link = f'&larr; <a href="{p_slug}.html">{p_label}</a>'
        if index < len(NAV) - 1:
            n_slug, n_label = NAV[index + 1]
            next_link = f'<a href="{n_slug}.html">{n_label}</a> &rarr;'

        page = SHELL.format(
            title=label,
            description=description,
            sidebar=sidebar_html(slug),
            body=body.replace("REPOURL", REPO).rstrip(),
            prev=prev_link,
            next=next_link,
            repo=REPO,
        )
        (out / f"{slug}.html").write_text(page, encoding="ascii")
        print(f"wrote guide/{slug}.html ({len(page)} bytes)")

    print(f"\n{len(slugs)} pages generated")


if __name__ == "__main__":
    main()
