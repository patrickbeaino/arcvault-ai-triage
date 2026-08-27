# Architecture Write-Up

## System design

The system is a linear intake pipeline with one non-negotiable boundary: **probabilistic
components interpret; deterministic components decide.** The LLM converts unstructured
text into typed facts (category, priority, confidence, identifiers, two boolean impact
signals). Routing and escalation are pure functions over those facts — an auditor can read
~60 lines of JavaScript and know exactly why any request landed where it did, and the
business can change a threshold or a queue mapping without touching the model or prompt.

```
Client ──POST──▶ Webhook ▶ Normalize ▶ LLM (Ollama) ▶ Validate ▶ Route ▶ Escalate ▶ Notify ▶ Build ▶ Persist ▶ Respond
                 (n8n, Docker)         llama3.1:8b     │ enum/type/range checks    │ email via Mailpit
                                       schema-constrained decoding                 │ deterministic recipients
   INGESTION ──────────────▶ INTELLIGENCE ──────────▶ DECISION ───────────────────▶ ACTION
```

> The LLM interprets the request, while deterministic application logic controls
> routing, escalation, and downstream notifications.

Why the split matters:

- **LLMs are probabilistic** — the same message can classify differently across runs or
  model versions. Interpretation tolerates that; business decisions must not.
- **Routing and escalation must be predictable** — a support organization has to be able
  to say *exactly* why a ticket landed in a queue. `routing.rule` in every record is that
  answer.
- **Deterministic rules are testable and auditable** — the routing table and five
  escalation rules are ~60 lines of JavaScript that a reviewer can read in one sitting
  and change without touching a prompt or a model.
- **Low-confidence predictions have a safe landing** — below 0.70 the classification is
  treated as untrusted and the request goes to human review, so model uncertainty
  degrades into human work, never into silent misrouting.

## Components and how they connect

| Component | Role | Connection |
|---|---|---|
| n8n (Docker, port 5678) | Orchestrator; all 9 nodes | Webhook in, HTTP out to Ollama, bind-mounted `./output` for persistence |
| Ollama + llama3.1:8b (host process) | Classification/extraction | `host.docker.internal:11434`; kept on the host for Apple-Silicon GPU access |
| `tools/build_workflow.py` | Workflow-as-code | Generates the importable workflow JSON, injecting the versioned prompt file |
| Mailpit (Docker, port 8025) | Local email capture: SMTP-less send API + browser inbox | Called by the Send Email Notification node at `mailpit:8025`; no accounts or credentials, mail never leaves the machine |
| `output/` | Persistence | One JSON record per request, written by the workflow |

**Trigger mechanism**: a production-mode n8n webhook (`POST /webhook/arcvault-intake`).
Synchronous request/response was chosen deliberately: at assessment scale it makes the
demo self-verifying (the caller gets the full processed record back), and the ~3s LLM
latency is acceptable. The same workflow behind a queue is the production evolution (below).

**State / persistence**: the pipeline itself is stateless per request. Two stores exist:
n8n's own execution history (SQLite in the `n8n_data` volume — free observability: every
run is replayable node-by-node in the UI) and the durable business record in
`output/<request_id>.json`. A flat file per request was chosen over a database because the
assessment needs inspectable records, not queries; the Persist node is the single seam
where Postgres/S3 would slot in.

**Routing logic**: static category→queue table (Bug Report→Engineering, Feature
Request→Product, Billing Issue→Billing, Technical Question→IT/Security,
Incident/Outage→Engineering), with one gate in front: if validation failed or confidence
< 0.70, destination becomes "Human Review / Escalation" — an untrusted classification must
not choose a queue. Each record carries `routing.rule` naming which branch fired. I kept
the assessment's table as-is; the one debatable mapping (Technical Question → IT/Security
rather than a general Support queue) is noted but not "improved", since renaming queues is
a product decision, not an engineering one.

**Escalation logic**: the five assessment rules, evaluated independently so
`escalation.reason` lists *every* rule that fired (req-005 shows three). Rule 5 is the
clearest expression of the design boundary: the LLM extracts `amount_charged_usd` and
`amount_expected_usd` as stated in the text; code computes `|1240 − 980| = 260` and
compares to 500. An 8B model asked to subtract is a liability; a subtraction in code is a
fact. Validation failure is an additional escalation reason (required by the assessment's
validation section): bad LLM output degrades to human review, never to silent bad data or
a crashed workflow.

One deliberate deviation from the brief: Step 6 suggests escalated records be routed to a
separate escalation queue *instead of* their standard destination. Here, escalation and
routing are decoupled on purpose. The escalation flag answers "does a human need to look?";
the destination answers "whose problem is this?" — and for a confident classification both
answers are useful at once: req-005's outage stays in Engineering's queue (they must act
*now*) while the flag, the ⚠ email subject, and the HUMAN REVIEW REQUIRED block summon
human attention. Only when the classification itself is untrusted (confidence < 0.70,
failed validation) does the destination change to Human Review, because then no team
assignment can be trusted either. Collapsing both meanings into one queue — the literal
Step 6 — is a two-line change in Determine Route if an operation prefers it; keeping them
separate preserves information the merged version throws away.

**Downstream action — email notification**: after routing and escalation are decided, the
workflow notifies the owning team. Three design rules keep it safe and explainable:

1. *Recipients are code, not model output.* A Set node ("Load Email Config") — evaluated by
   the n8n core process, where `$env` is available — injects an env-configurable
   destination→address map; the Compose node indexes that map with `routing.destination`.
   The LLM's schema has no recipient field, so neither the model nor a malicious customer
   message can redirect a notification (proven by the req-007 injection test).
2. *One email per request.* The recipient always follows the routing destination; escalation
   decorates the email (⚠ subject marker + "HUMAN REVIEW REQUIRED" section with the fired
   rules) rather than adding a second send. Low-confidence requests are routed to Human
   Review, so their single email naturally goes to human-review@.
3. *Failure cannot destroy the ticket.* The send is wrapped in try/catch with a 5s timeout;
   the record persists regardless, carrying `notification.email.status`
   (`sent` / `failed` / `disabled`) so the outcome is auditable. Local delivery uses
   Mailpit's HTTP send API — in production this node is the seam where SES/SMTP (or a
   Jira/Zendesk ticket-create call) plugs in.

## Reliability

- **Schema-constrained decoding** (Ollama `format`) makes malformed JSON impossible at the
  source; the Validate node re-checks enums, types, and ranges anyway (grammar cannot
  enforce `0 ≤ confidence ≤ 1`), and its failure path is human review, not an exception.
- The webhook rejects messageless payloads; the HTTP node has a 120s timeout.
- Honest gaps at this scale: no retry on Ollama failure (n8n node-level retry is a
  checkbox — left off to keep demo behavior transparent), no idempotency (re-sending
  req-001 reprocesses it and overwrites the record — which is at least convergent), no
  dead-letter store beyond n8n's execution log.

## Cost & latency

Local inference: marginal cost per request is ~0 (electricity); the "cost" is fixed
hardware and ~5GB of weights. Latency measured end-to-end: **~2.5–3s warm**, +~5s model
load on first request (`keep_alive: 30m` prevents mid-demo cold starts). A hosted small
model (e.g. Claude Haiku via API/Bedrock) would cost fractions of a cent per request at
this token count (~1k in / ~250 out) and halve latency; the HTTP node makes that a
config change, not a redesign.

## What changes at production scale

Ordered by what I would actually do first:

1. **Queue-based processing**: webhook enqueues (SQS) and returns 202 + request_id;
   workers consume. Decouples intake availability from LLM latency, gives retries with
   backoff, a real dead-letter queue for poison messages, and idempotency keyed on
   request_id (dedupe on the queue + upsert on the store).
2. **Real persistence**: Postgres (or DynamoDB) for records — queryable by queue, category,
   escalation state; S3 for raw payload archival. The Persist node is the only seam that changes.
3. **Hosted LLM with fallback**: primary hosted model, fallback to a second provider (or
   the local model) on timeout/5xx; circuit breaker; per-request `llm_model` already
   recorded, so mixed-provider fleets stay auditable.
4. **Observability**: structured logs per node (request_id, prompt_version, model,
   latency, validation result, rules fired), metrics on category/confidence/escalation-rate
   distributions — a drifting escalation rate is the canary for prompt or traffic drift —
   and alerts on validation-failure spikes.
5. **Security/PII**: authentication on the webhook (HMAC signature or at minimum a header
   token — trivial in n8n, skipped per the brief), TLS termination, PII redaction before
   logs, retention policy on raw messages, and prompt-injection review: constrained
   decoding already caps output to legal field values, but extracted *values* can still be
   adversarial ("all users are down"), so high-impact signals should require quoted
   evidence verified against the source text.
6. **Model & prompt lifecycle**: prompts already versioned in files and stamped into every
   record (`meta.prompt_version`); add a labeled eval set with per-category
   precision/recall gating prompt/model changes in CI, canary rollout, and a feedback loop
   where human corrections from the escalation queue become new eval cases.
7. **Cost/rate controls**: per-tenant rate limits at the gateway, token budget alarms,
   response caching only if duplicate messages are common (they are, via email threads).

## Phase 2 improvements (product, not plumbing)

- Confidence from evidence (logprob margin or verifier pass) instead of self-report.
- Multi-label handling: req-006-style mixed messages could produce a secondary category
  hint for the human reviewer instead of just lower confidence.
- Auto-acknowledgement email to the customer using the generated summary.
- Feedback UI for the Human Review queue that writes corrections back to the eval set —
  closing the loop that actually improves the classifier over time.
