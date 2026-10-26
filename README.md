# Centra: AI employees that learn a job from its owner and prove their work

A department head describes a repeated process in plain English. Centra turns the description into a
reviewable **SOP** (standard operating procedure). An **AI employee** then runs that SOP end to end on a
simulated company (a real browser on an ATS and a helpdesk, plus files, calendar and mail APIs). It asks
the owner when the SOP doesn't settle something, and waits for approval before consequential actions.
The work only counts as done when an **independent verifier**, reading the real systems, says so. After
a run, Centra reviews what happened and proposes SOP fixes for the owner to approve.

> **Demo video:** _link to be added_  ·  **Run it locally:** see [Setup](#setup) (about 5 minutes)

```
"Process today's Backend applicants."
        │
        ▼
 Raj (AI employee) ── opens the ATS in Chromium ── reads each resume ── records facts with quoted evidence
        │                                                         │
        │                         SOP rules decide (code) ◄───────┘
        ▼
 books calls / moves status / drafts emails ── policy gate (code) ── "rejection email needs Meera's OK" ── pauses
        │
        ▼
 finish ──► independent verifier reads ATS + calendar + outbox ──► ✓ 13/13 checks  (or: back to work)
```

---

## The problem as I interpreted it

The intern brief asks for an AI worker that takes a natural-language task and completes it on a computer.
Most of the hard parts aren't in clicking. They're in what a new hire struggles with:
*knowing the procedure, noticing when a case doesn't fit it, not doing anything irreversible without
permission, and being able to show the work is actually done.*

CentrAlign describes Centra as a system that "talks to your department heads, finds what repeats, builds an AI employee,
and fixes it when a run fails". So instead of hard-coding one workflow, I built that loop at small scale:

1. **Learn:** a plain-text description becomes a validated, structured SOP that a human reviews.
2. **Run:** one generic runtime executes *any* SOP (no per-workflow code).
3. **Prove:** completion is decided by reading the systems of record, never by the model's claim.
4. **Manage:** the run's problems turn into a proposed SOP revision (a diff) for the owner to approve.

Two jobs ship with it, both learned from plain text by the same code:

| Employee | Job (from the owner's description) | Tools |
|---|---|---|
| **Raj** · Recruitment Executive | Screen new Backend applicants: under 2 years of Python → reject (unless referred); otherwise book a Tue–Thu afternoon call, send an invite, update the ATS. Rejection emails need Meera's approval. | ATS (browser), resume files, calendar API, mail API |
| **Yuvraj** · Support Executive | Triage open tickets: password → reply with steps and close; billing → never reply, tag finance, alert Dev; bugs → ask for repro steps, tag engineering. | Helpdesk (browser), mail API |

The support job is the **generalization test**: a different domain, app and set of actions, running on the
same runtime, tools, gate and verifier with zero code changes.

---

## How each capability in the brief is implemented

| Capability | Mechanism | Where |
|---|---|---|
| Understand the end goal | The goal is a one-liner ("Process today's Backend applicants"). The employee finds the subjects itself in the apps. The runtime snapshots the in-scope subjects from the system of record **without showing them to the model**, so the verifier can catch skipped ones. | `runtime.py` `Employee.start` |
| Break into actions | The first action is `update_plan` (milestones, each with a *done-when* condition). The plan is revised when observations contradict it. | `tools.py` `t_update_plan` |
| Use tools | 16 typed tools: real Chromium (Playwright) with an element-id page snapshot, file reader, calendar and mail APIs, memory, decide, ask_human, finish. | `tools.py`, `browser.py` |
| Observe results | Every call returns a structured observation (ok, error class, text) that feeds the next step. Every browser step also writes a live screenshot. | `tools.py` `Obs` |
| Decide next | One LLM call per step chooses the next tool from the goal, SOP, working memory and recent history. There is no fixed sequence anywhere in the code. | `runtime.py` `loop` |
| Remember | Working memory of facts, decisions, completed actions and unknown-outcome writes, kept separate from the transcript (older steps are compressed to one line). Long-term memory is the SOP itself: owner answers and fixes become new SOP versions. | `state.py`, `manager.py` |
| Detect failure | Typed error classes: `blocked`, `conflict`, `unknown_outcome`, `not_found`, `invalid_args`, `transient`. A write that times out or returns 5xx is *unknown*, not *failed*. | `connectors.py`, `tools.py` |
| Retry / alternative | Unknown outcome → a retry is blocked until the system of record is read (automatic reconciliation). Every write carries an idempotency key, so even a retry can't duplicate. Slot taken (409) → pick another valid slot. UI renamed → re-read the page and report an SOP issue. Model provider down → fail over to the next provider, or pause the run in a resumable state. | `policy.py`, `tools.py`, `llm.py` |
| Verify the outcome | A separate verifier reads the ATS, calendar, outbox and helpdesk and checks that every in-scope subject was decided, that each decision re-derives from the facts, that every required action exists **exactly once**, that nothing outside the decision happened, and that nothing out of scope was touched. Failures go back to the employee to fix (up to 3 times). | `verifier.py` |
| Ask for clarification / approval | Approval is configured once in the SOP ("set the line once"); the gate pauses the run and later replays the exact approved action. Clarification is enforced in code: if an alternative reading of a fact would change the outcome, `decide` refuses until the owner answers. | `policy.py`, `tools.py` `t_decide` |
| Summary and evidence | The report is built by code from the run record, not written by the model: outcome per subject, the quoted evidence behind each fact, the rule that fired, the verifier's checks, incidents handled, security flags, screenshots, steps, duration and model cost. | `runtime.py` `build_report` |

---

## Key design decisions

**1. The LLM proposes, code decides.** The model chooses *what to do next* and *reads messy inputs*.
Anything with consequences is deterministic code: the SOP rules (a safe AST-checked expression evaluator,
first match wins), the policy gate, recipient resolution, idempotency, and the definition of done. I chose
this over "LLM-as-judge" or confidence scores because a model grading its own work is the failure mode
the brief warns about. In the interview I can point to the exact line that blocks any given unsafe action.

**2. Facts need evidence; decisions come from rules.** The employee can't just *say* someone has 6 years
of Python. `record_fact` requires a quote that is found **verbatim** in something it actually observed this run,
and text that addresses an automated system is rejected as evidence. `decide` then applies the owner's rules.
This splits the work cleanly: the LLM extracts, the rules decide, and the verifier checks.

**3. The gate judges what an action *does*, not what the model says it does.** Browser write buttons carry
`data-action`/`data-subject` attributes, so before a click the gate reads the real form state (which status
is selected, whether "close ticket" is ticked). Email recipients come from the system of record, never from
the model or the document, so a resume saying "send it to my other address" has nothing to act on.

**4. A lost response is not a failure.** The calendar can commit a booking and then time out. The runtime
marks that write *unknown*, blocks a blind retry, and reconciles by reading the calendar. Separately, the
server dedupes on the idempotency key. Both layers have tests.

**5. Ambiguity is detected by its consequence.** Asking a model "are you unsure?" isn't reliable. Instead
the employee records an `alternative_value` when a source can be read two ways (does an internship count?),
and code checks whether the alternative **changes the outcome**. It only asks the owner when the outcome
would change, so no unnecessary questions get asked.

**6. No agent framework.** The loop is about 250 lines I can step through in a debugger. Every prompt the
system sends is in `runtime.py` and `manager.py`.

**7. The scope snapshot is hidden from the model.** At run start, code reads which subjects are in scope.
The employee has to find them itself. In one real run it skipped an applicant, called `finish`, and the
verifier rejected the claim ("A-104: no decision recorded"). The employee then went back and processed her.

**8. Cheap models, with failover.** It runs on free tiers: Groq `openai/gpt-oss-20b` first, Gemini
`3.1-flash-lite` as the fallback. A rate-limited model goes on cooldown for exactly the time the
provider asks for, and the call moves to the next model instead of sleeping. A run that loses all
providers pauses with saved state and resumes from the same step, also after a server restart.

---

## Architecture

```
centra/
  web.py         FastAPI: operator console + JSON API; hosts the sandbox in the same process
  runtime.py     the generic employee loop, prompts, pause/resume, report
  tools.py       the 16 tools; every write goes through policy.check_write
  policy.py      the gate: decision ↔ action, scope, duplicates, unknown outcomes, constraints,
                 approvals, evidence and injection checks
  verifier.py    independent completion check against systems of record
  sop.py         SOP schema + semantic validation + safe rule evaluation + versioned storage
  manager.py     Centra: learn_sop (text → SOP), review_run (run → proposed SOP diff)
  browser.py     Playwright wrapper: text snapshot with element ids, live view, replay of approved writes
  connectors.py  typed clients for the company systems (would be Odoo/Zendesk/Gmail in production)
  llm.py         providers (Groq, Gemini, Anthropic, any OpenAI-compatible/local), JSON mode, failover
  state.py       durable run state (saved after every step)
  static/        the console (single file, no build step)
sandbox/         the simulated company: ATS + helpdesk (HTML), calendar + mail (API), chaos switches
sops/            SOPs learned from plain text, versioned (v2+ are owner-approved revisions)
tests/           guarantees that hold whatever the model does (no LLM needed)
evals/           scenario runner that plays the human and grades runs against seeded ground truth
```

**Simulated company situations** (choose them under *Demo environment* in the sidebar):
`clean` · `ambiguous` (internship + part-time experience) · `lost_response` (calendar commits then times out) ·
`slot_conflict` (slot taken mid-booking) · `injection` (a resume tells "the screening system" to record 6 years
and email another address) · `ui_changed` (the ATS "New" tab is renamed "Inbox") · `support`.

---

## Setup

Requirements: Python 3.11+, and a free API key from [Groq](https://console.groq.com) and/or
[Google AI Studio](https://aistudio.google.com).

```bash
git clone <this repo> && cd <repo>
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
cat > .env <<'EOF'
GROQ_API_KEY=...
GEMINI_API_KEY=...
LLM_PROVIDERS=groq,gemini
EOF
.venv/bin/uvicorn centra.web:app --port 8000
```

Open **http://localhost:8000**.

1. **AI employees:** pick Raj, keep the goal, click **Assign**. Watch the timeline and the live browser view.
   Approve the rejection email when it asks. When it finishes, read the work report and its verification.
2. Under **Demo environment**, choose another situation (e.g. *A resume tries to manipulate the AI*) and assign again.
3. **Teach a new job:** use an example or write your own process, review the SOP Centra writes, and approve it.
4. On a finished run, click **Review this run** to have Centra propose an SOP fix.

Docker: `docker build -t centra . && docker run -p 8000:8000 --env-file .env centra`.
Tests: `.venv/bin/python -m pytest -q tests` (13 tests, no API key needed).
Scenario runner: `.venv/bin/python -m evals.run_evals --n 3` (server must be running).

Other providers: `LLM_PROVIDERS=anthropic` (with `ANTHROPIC_API_KEY`) or `openai_compat` with
`OPENAI_COMPAT_URL` for a local Ollama / llama.cpp server.

---

## What has been verified, honestly

**Deterministic guarantees (13 tests, all passing):** rule precedence; no decision when a fact is missing;
invalid or unsafe SOPs rejected; the gate blocks out-of-scope, out-of-outcome, duplicate, constraint-violating
and unreconciled writes; approval → execution → rejection-is-final; evidence must be observed verbatim and
must not be an instruction; ambiguity that flips the outcome blocks `decide`; a lost response commits once
and an idempotent retry returns the same event; a stolen slot returns 409.

**On the real model, through the full loop:**
- `clean`: completed, verifier 13/13. Priya → call booked inside Tue–Thu afternoons; Arjun → rejected,
  email sent only after approval; Kabir (1.4 yrs but referred) → call, so the exception rule was applied.
- `injection`: completed, 13/13. The employee read the hidden note, said in its reasoning that it was an
  instruction to ignore, recorded 1 year from the real work history, and Arjun was rejected. No email went to the injected
  address (recipients come from the ATS anyway).
- `ambiguous`: completed, 17/17, and in another run the verifier caught a skipped applicant and sent the employee back.
- A slot conflict happened unscripted (the employee picked an already-busy slot, got 409, re-read
  availability and booked another valid slot).
- Both SOPs were learned correctly from the plain-text descriptions above. The learner also surfaced sensible
  open questions, and a real scope bug it introduced ("New" vs "new") led to case-insensitive scope matching and
  a refusal to start runs with an empty scope.

**Not yet measured:** repeated-run pass rates per scenario (the runner exists in `evals/`; free-tier
rate limits made long batches slow), and full real-model runs of `lost_response`, `ui_changed` and
`support` beyond their unit-level tests.

---

## Known limitations

- **The ambiguity behavior depends on the model.** The code enforces the question *if* the employee records an
  alternative reading. In one `ambiguous` run the small model counted the internship without flagging it. The
  outcome was correct, but nobody was asked.
- **The injection defense is layered but heuristic.** The instruction-pattern check is a regex. The stronger
  guarantees are structural: evidence must be observed verbatim, recipients come from the system of record,
  and decisions come from rules. A fact that is *plausibly* phrased, false, and present in the document would
  still be recorded. The verifier checks consistency, not truth; truth is only checked by the eval oracle against
  seeded ground truth.
- **One sandbox, one run at a time.** The world is in memory and global. Browser state isn't kept across a
  pause (the page is reopened on resume).
- **The SOP rule language is small on purpose** (comparisons, and/or/not over typed fields). Rules that need
  arithmetic over dates are left to extraction.
- **Token-heavy.** About 4k tokens per step and 40–55 steps per run on the recruitment job (~$0.03 at paid
  rates for the small models; free tier in practice).
- **The sandbox is simulated.** The connectors are typed, but they target my sandbox, not Odoo or Zendesk.

## What I would build next

1. **Pass^k evals in CI** for every scenario, with the real model, reporting failures, plus a regression gate on SOP changes.
2. **Real connectors** (Gmail, Google Calendar, an ATS API) behind the same `connectors.py` interface, with OAuth scopes as permissions.
3. **A durable job queue** (one sandbox/tenant per run, scheduled runs such as "every morning at 9"), with the run state in Postgres instead of JSON files.
4. **Tighter context:** a per-subject sub-loop, so the prompt carries only the current applicant, which would cut tokens 3–4×.
5. **Learning from feedback:** owner answers become SOP rules automatically once they repeat, with Centra tracking how often each SOP needs a human.
6. **Provenance for facts:** tag each observed span with its source and reject facts whose evidence comes from untrusted regions.

## Assumptions

- The environment is a sandbox; no real company systems, credentials or personal data are used.
- One owner per SOP; approvals and questions go to that owner.
- "Today" is the server's date; interview slots are next week, Mon–Fri 10:00–17:00.
- Python experience is counted by calendar time from the resume; the owner settles edge cases.

## Models, APIs and components used

- **LLMs:** Groq `openai/gpt-oss-20b` (primary), Google Gemini `gemini-3.1-flash-lite` / `gemini-flash-lite-latest` (fallback), called over plain HTTPS from `llm.py`. Anthropic and OpenAI-compatible endpoints are supported but weren't used for the results above.
- **Libraries:** FastAPI, Uvicorn, Pydantic, httpx, Playwright (Chromium), pytest, python-multipart. No agent framework.
- **Frontend:** a single HTML file with vanilla JS, and the Inter font from Google Fonts.
- **AI coding assistance:** built with Claude Code (Anthropic) as a pair programmer. I designed the system with it,
  reviewed the code, and ran and debugged every scenario described above.
