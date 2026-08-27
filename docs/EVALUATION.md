# Evaluation Guide

For the Valsoft reviewer. Two paths: a **fast evaluation** that needs no installation,
and a **full run** that takes about 15 minutes including the model download.

## What this project demonstrates

- A working AI intake-and-triage pipeline in n8n: webhook → LLM classification/enrichment
  → validation → deterministic routing → deterministic escalation → persisted JSON record
  → email notification to the owning team.
- The core design principle: **the LLM interprets unstructured requests into structured
  data; deterministic application logic makes every decision** (validation, routing,
  escalation, notification recipients). Rationale in [ARCHITECTURE.md](ARCHITECTURE.md).
- Guardrails that treat the LLM as untrusted: schema-constrained decoding, a validation
  node that re-checks every field, a 0.70 confidence gate, arithmetic done in code (the
  $500 billing rule), and an anti-hallucination extraction contract — including a test
  proving email recipients cannot be influenced by the message content.
- Honest, documented AI-assisted development: four prompt-iteration failures and three
  platform corrections, each with root cause and fix ([TESTING.md](TESTING.md),
  [PROMPTS.md](PROMPTS.md)).

## Fast evaluation (no installation)

Everything needed to judge the work is committed:

1. **`ui/demo.html`** — open it directly in any browser: a self-contained replay of the
   customer portal and triage board over the seven recorded test runs (real LLM output,
   routing, escalations, notifications). Nothing needs to be installed or running.
2. **README.md** — architecture, routing/escalation rules, example output.
3. **`structured-output.json`** (repo root) — all seven records in one file, or
   **`output/req-001.json` … `req-007.json`** — the same records as the workflow wrote them
   (not hand-written): classification, extracted identifiers, routing with the rule that
   fired, escalation reasons, email delivery status.
4. **`workflow/arcvault-intake.json`** — the complete n8n workflow export; every Code
   node's logic is readable in the JSON, or import it into any n8n instance to view the
   canvas.
5. **`prompts/classify_v1.txt` → `classify_v5.txt`** — the full prompt evolution;
   [PROMPTS.md](PROMPTS.md) explains what failure drove each version.
6. **`tests/requests/`** — the five assessment inputs plus two extra probes (an ambiguous
   message and a recipient-injection attempt).
7. **`docs/screenshots/`** — the workflow in action, one image per assessment step
   (captured from real executions of req-005, the escalated outage, and req-007, the
   injection probe):
   - Step 1 · Ingestion — [the 12-node canvas with the webhook trigger](screenshots/n8n-workflow-canvas.jpg)
     and [a succeeded end-to-end execution](screenshots/n8n-execution-succeeded.jpg)
   - Steps 2–3 · Classification & enrichment — [the Analyze with LLM node: input, Ollama call, raw schema-constrained output](screenshots/step2-3-analyze-with-llm.jpg)
   - Step 4 · Routing — [Determine Route during the injection test: routing-table code, Billing output](screenshots/step4-determine-route.jpg)
   - Step 5 · Structured output — [Build Final Record producing req-005.json](screenshots/step5-build-final-record.jpg)
   - Step 6 · Escalation — [Determine Escalation with both outage signals true](screenshots/step6-determine-escalation.jpg)
   - Downstream action — [the Send Email node](screenshots/action-send-email.jpg) and
     [the captured inbox, one email per request](screenshots/mailpit-inbox.jpg)
   - Bonus — [the live triage board](screenshots/triage-board.jpg)

## Full run

Follow [QUICK_START.md](QUICK_START.md) (Docker Desktop + Ollama + `llama3.1:8b`;
Windows, macOS, and Linux instructions included). Then:

- macOS/Linux: `./tests/run_tests.sh` — Windows: `.\tests\run_tests.ps1`

## The five assessment cases and expected results

| Input | Expected | Verified result |
|---|---|---|
| req-001 login 403 | Bug Report, High reasonable, 403 + account extracted, Engineering, no auto-escalation | ✓ all (confidence 0.9) |
| req-002 bulk export ask | Feature Request, Product, Med/Low, no escalation | ✓ (Low, 0.9) |
| req-003 invoice #8821, $1,240 vs $980 | Billing Issue, invoice extracted, Billing, $260 ⇒ **no** escalation ($500 threshold) | ✓ (discrepancy computed in code) |
| req-004 Okta SSO question | Technical Question, IT/Security, Okta extracted, no escalation | ✓ |
| req-005 dashboard down, multiple users | Incident/Outage, High, Engineering, escalation **true** | ✓ (3 reasons listed) |

Bonus probes: req-006 (vague message → confidence 0.6 → routed to Human Review) and
req-007 (message demands emailing the CEO and a Gmail address → notification still goes to
billing@arcvault.local, proving recipients come from routing logic, not the LLM or the
message).

## How to inspect routing

- Every record carries `routing.destination` **and** `routing.rule` — the exact rule that
  chose the queue (`category_routing_table`, `confidence_below_0.70`,
  `llm_output_failed_validation`, or `human_override` after a human re-routes).
- The routing table itself is ~20 lines in the "Determine Route" Code node
  (see `tools/build_workflow.py`, `ROUTE_JS`).
- In the n8n UI: Executions tab → any run → click "Determine Route" to see its exact
  input and output.

## How to inspect escalation

- `escalation.flagged` + `escalation.reason` in every record — the reason concatenates
  every rule that fired (req-005 shows all three of its reasons).
- The five rules are ~40 lines in the "Determine Escalation" Code node (`ESCALATION_JS`).
- The captured email for an escalated request (http://localhost:8025) carries ⚠ in the
  subject and a "HUMAN REVIEW REQUIRED" block.

## Suggested live-demo / recording walkthrough (~4 minutes)

1. **The principle** (30s): n8n canvas at http://localhost:5678 — point at the boundary:
   one LLM node, everything after it deterministic Code nodes.
2. **Run the five cases** (60s): `./tests/run_tests.sh` in a terminal; each line prints
   classification, route, escalation, and email delivery.
3. **One record end-to-end** (60s): open the req-005 execution in n8n (Executions tab) and
   step through Analyze → Validate → Route → Escalate node outputs; then open
   `output/req-005.json`.
4. **The action** (30s): http://localhost:8025 — one email per request, correct team,
   ⚠ + HUMAN REVIEW REQUIRED on the escalated ones.
5. **Bonus, if time** (60s): the board at http://localhost:8090 — submit a fresh message
   with "✚ New request", watch it route live; show req-007's drawer (the injection
   attempt) and one human re-route from the Human Review queue.

## Where things live

| Artifact | Location |
|---|---|
| Architecture write-up (incl. production evolution) | `docs/ARCHITECTURE.md` |
| Prompt design + version history | `docs/PROMPTS.md` |
| Test log incl. what AI got wrong and fixes | `docs/TESTING.md` |
| Plain-English product tour | `docs/USER_GUIDE.md` |
| Workflow source of truth | `tools/build_workflow.py` (generates the workflow JSON, injecting the versioned prompt) |
| Bonus demo board (optional) | `ui/` — `python3 ui/server.py` → http://localhost:8090 |
