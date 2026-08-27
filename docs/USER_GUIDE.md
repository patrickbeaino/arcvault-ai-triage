# User Guide

*A plain-English tour. For installation, see [QUICK_START.md](QUICK_START.md). For a
technical deep-dive, see [ARCHITECTURE.md](ARCHITECTURE.md).*

## What is ArcVault AI Triage?

ArcVault is a (fictional) B2B software company that receives customer messages through
email, web forms, and a support portal — bug reports, feature requests, billing questions,
outage alerts — all as free-form text. This system reads each message, understands what
it's about, and delivers it to the right team, flagging anything that needs a human's
judgment. The team even gets an email about it.

The whole journey:

```
Customer Request
       ↓
AI Understanding            the message is read and interpreted
       ↓
Structured Information      category, priority, IDs, urgency — as clean data
       ↓
Business Rules              fixed, predictable rules check the data
       ↓
Routing                     the request is assigned to a team queue
       ↓
Human Escalation if needed  uncertain or critical cases are flagged for a person
       ↓
Structured Record + Email   a JSON record is saved; the owning team is notified
```

One principle runs through everything: **the AI only reads and understands — it never
decides**. Which queue a request goes to, whether a human gets pulled in, and who receives
the email are all decided by simple rules anyone can read and audit.

## How do I submit a request?

Four ways, easiest first:

1. **The customer portal** (`python3 ui/server.py` → http://localhost:8090/portal): the
   page a customer would actually see. Describe a problem in your own words and press
   **Submit request** — you get back a reference ID, the team now handling it, a response
   promise based on priority, and an expandable "what happened behind the scenes" trace.
2. **The demo board** (http://localhost:8090): the ops-side view. Type or paste a
   customer message in the box at the top, pick a source channel, and press
   **Route request**. You'll watch it get analyzed and land in a queue a few seconds later.
3. **The test scripts**: `./tests/run_tests.sh` (macOS/Linux) or `.\tests\run_tests.ps1`
   (Windows) send all the sample requests at once.
4. **Any HTTP client** (curl, PowerShell, Postman): POST JSON like
   `{"source": "email", "message": "your text here"}` to
   `http://localhost:5678/webhook/arcvault-intake`.

No stack running at all? Open `ui/demo.html` in any browser — a self-contained replay of
the seven recorded test runs, portal → pipeline → triage board, with real LLM output.

## What happens to my request?

1. It's cleaned up and given an ID.
2. The AI reads it and fills out a form: what kind of request is this, how urgent, what
   account/invoice/error identifiers are mentioned, is more than one user affected — and
   how confident it is in its own answer.
3. That form is checked — if it's malformed or the AI's confidence is low, the request
   goes to a person instead of being trusted.
4. Fixed rules route it: bug reports and outages → Engineering, feature ideas → Product,
   money questions → Billing, how-to questions → IT/Security.
5. Fixed rules decide whether a human must look at it (see below).
6. A permanent JSON record is saved, and an email notification goes to the owning team.

## What does the output mean?

Every request produces one record (in `output/`, and returned by the webhook):

| Field | Meaning |
|---|---|
| `classification.category` | what kind of request the AI judged it to be |
| `classification.priority` | High / Medium / Low |
| `classification.confidence` | how sure the AI is (0–1). Below **0.70**, humans take over |
| `enrichment.identifiers` | account IDs, invoice numbers, error codes found *in the message* — never invented; `null` means "not mentioned" |
| `enrichment.billing_discrepancy_usd` | difference between charged and expected amounts, calculated by code, not the AI |
| `routing.destination` | the team queue it was assigned to |
| `routing.rule` | *why* — which rule made that assignment |
| `escalation.flagged` / `reason` | whether a human must review it, and exactly why |
| `notification.email` | who was emailed about it, and whether delivery succeeded |
| `summary` | one or two sentences a support agent can read instead of the raw message |

## How do I identify an escalation?

A request is escalated when any of these is true: the AI's confidence is below 0.70, the
request describes an outage, multiple users are affected, a critical service-wide failure
is indicated, or a billing discrepancy exceeds $500.

You can spot one in four places:

- In the record: `"escalation": { "flagged": true, "reason": "…" }` — the reason names
  every rule that fired, in plain English.
- On the demo board: a red **Escalated** badge on the ticket; the "escalated" counter at
  the top is clickable to see them all.
- In the email: the subject carries **⚠** and the body says **HUMAN REVIEW REQUIRED**.
- Low-confidence cases go one step further: they land in the **Human Review / Escalation**
  queue itself, where (on the demo board) a person can read the message and send it to the
  right team with one click.

## Where do the emails go?

Nowhere on the internet — they're captured locally by Mailpit so you can see exactly what
each team *would* receive. Open **http://localhost:8025** to read them. Each request
produces exactly one email, addressed to the team that owns its queue.
