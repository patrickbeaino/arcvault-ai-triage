# Testing Log

Method: iterate the prompt against Ollama directly (fast, seconds per sweep), then run
everything end-to-end through the live n8n workflow via `tests/run_tests.sh`
(macOS/Linux, uses jq) or `tests/run_tests.ps1` (Windows PowerShell, no dependencies —
verified end-to-end with PowerShell 7.6.5 against the live workflow, including UTF-8
round-tripping of typographic characters in the test messages). Expected
outcomes for the five assessment cases come from the assessment brief; req-006 is a
deliberately ambiguous bonus case added to prove the low-confidence → Human Review path
actually fires (the five official cases never trip it).

## Iteration history — what failed and what fixed it

### Round 1 (prompt v1): the prompt contaminated its own output

req-001 ("I tried logging in… 403"):

```json
"urgency_signal": "This morning, multiple users affected.",
"signals": { "multiple_users_affected": true, ... }
```

The message mentions one user. The phrase "multiple users affected" came verbatim from the
*example inside the v1 prompt* ("e.g. stopped loading around 2pm EST, multiple users
affected"). Impact: false escalation of a routine bug report (rule 3).
**Fix (v2):** removed the concrete example; required urgency_signal to use only words from
the message; added "first person singular ⇒ false" to `multiple_users_affected`.

### Round 2 (prompt v2): single blocked user flagged as critical failure

req-001 now had `multiple_users_affected: false` ✓ but `critical_system_failure: true` ✗ —
one person's 403 is not a critical system failure, and this would again falsely escalate
req-001 (rule 4). All other cases correct.
**Fix attempt (v3):** rewrote the definition to require org-wide, outage-level impact.

### Round 3 (prompt v3): definitions alone did not work

req-001 *still* returned `critical_system_failure: true`. Diagnosis: with
schema-constrained decoding the model regenerates the field name "critical_system_failure"
immediately before choosing true/false, and that name pattern-matches "user is completely
blocked" no matter what the definition three paragraphs earlier says.
**Fix (v4):** renamed the field to `service_wide_outage` in both schema and prompt, with
negative criteria first ("false whenever one individual reports their own error…").
Result: req-001 correct on the next run and stable since. Lesson: **under constrained
decoding, field names are part of the prompt** — often a stronger part than definitions.

### Confidence calibration (v3–v4, partially solved, honestly documented)

The model's self-reported confidence clusters at 0.8–1.0. Adding numeric bands (v3) and
"reporting 0.6 is honest; reporting 0.9 is a failure" (v4) got a genuinely vague message
("please advise on the thing we discussed earlier") down to **0.6**, which correctly routes
to Human Review. But probing showed the limit: "Hi, following up on my last message. Any
update?" — which contains zero classifiable content — still gets 0.9 ("Technical
Question"). Verbalized confidence on an 8B model catches obvious ambiguity, not subtle
overreach. Production mitigation (documented in ARCHITECTURE.md): logprob margins or a
second verifier pass, plus sampled human review of non-escalated records.

### Round 5 (prompt v5): question-phrased failure reports misclassified

Found while probing beyond the official cases: *"Since about 9am our nightly archive
export fails with error E-501. … Can you check what changed?"* came back **Technical
Question (0.89) → IT/Security**. The polite question framing matched the
question tie-breaker harder than the failure report matched Bug Report — and at 0.89
confidence, the low-confidence gate could not catch it (miscalibration again).
**Fix (v5):** new tie-breaker — "a report that existing functionality is failing or
producing an error is a Bug Report even when phrased politely as a question."
Regression: all six existing cases re-run and still pass (notably req-004, a genuine
question, still classifies as Technical Question); the probe now returns
Bug Report → Engineering with `error_code: "E-501"` extracted.

### Round 6 (prompt v6): summary length brought up to spec

Not a model failure — a spec-compliance fix. The assessment asks for a "human-readable
summary (2–3 sentences)"; v5's instruction said "one or two sentences" and mostly produced
one. v6 rewords the summary as a handoff note ("what happened, any impact or identifiers
that matter, and what the customer needs next"). Full regression re-run: every
classification, route, and escalation identical across all seven cases; the five
assessment cases now carry 2–3 sentence summaries (req-006, the deliberately content-free
probe, stays at one sentence — there is nothing more to say about it).

### Platform failures found while wiring n8n (not LLM issues)

| Symptom | Cause | Fix |
|---|---|---|
| `SQLITE_CONSTRAINT: NOT NULL … workflow_entity.id` on CLI import | n8n 2.x requires an explicit workflow `id` in the JSON | fixed id `ArcVaultIntake01` in the builder |
| `update:workflow --all` refused | removed in n8n 2.x | `n8n publish:workflow --id=…` |
| Webhook returned 200 but empty body, nothing persisted, logs full of `Access to the file is not allowed` | n8n 2.x blocks all filesystem writes by default | `N8N_RESTRICT_FILE_ACCESS_TO=/data/output` in docker-compose |
| n8n won't run natively on the host | host has Node 25 (non-LTS, unsupported) | run n8n in Docker |

## Final end-to-end results (prompt v6, through the live workflow)

All requests sent by `tests/run_tests.sh` to `POST /webhook/arcvault-intake`; full records
in `output/`, raw responses in `tests/responses/`.

| Request | Expected (from brief) | Actual | Pass |
|---|---|---|---|
| req-001 | Bug Report, High reasonable, 403 + account extracted, Engineering, **no** auto-escalation | Bug Report / High / 0.9 → Engineering, `error_code:"403"`, `account_id:"arcvault.io/user/jsmith"`, not escalated | ✓ |
| req-002 | Feature Request, Product, Med/Low, no escalation | Feature Request / Low / 0.9 → Product, not escalated | ✓ |
| req-003 | Billing Issue, invoice 8821, Billing, $260 discrepancy ⇒ **no** escalation (threshold $500) | Billing Issue / Medium / 0.9 → Billing, `invoice_number:"8821"`, amounts 1240/980 extracted, `billing_discrepancy_usd: 260` computed in code, not escalated | ✓ |
| req-004 | Technical Question, IT/Security, Med/Low, Okta extracted, no escalation | Technical Question / Low / 0.9 → IT/Security, `external_systems:["Okta"]`, not escalated | ✓ |
| req-005 | Incident/Outage, High, multiple users, Engineering, escalation **true** | Incident/Outage / High / 0.9 → Engineering, both signals true, escalated with 3 explicit reasons | ✓ |
| req-006 (bonus) | ambiguous ⇒ confidence < 0.70 ⇒ Human Review | 0.6 → "Human Review / Escalation", escalated: "Classification confidence 0.60 is below the 0.7 threshold" | ✓ |

Latency: ~2.5–3s per request end-to-end (llama3.1:8b on an M4 Max, model kept warm via
`keep_alive: 30m`; first request after a cold start adds ~5s of model load).

Additionally, every escalation rule has been exercised by a live run: rules 2–4 by
req-005, rule 1 by req-006, and rule 5 by an ad-hoc submission through the demo board
("we were double charged $2,400 … our plan is $1,200/month") — amounts extracted 2400/1200,
discrepancy $1,200 computed in code, > $500 ⇒ escalated, routed to Billing at High priority.

## Email notification tests (downstream action)

All verified against the live workflow with Mailpit capturing delivery
(`./tests/check_inbox.sh` lists the inbox):

| Test | Result |
|---|---|
| req-001 Bug Report | one email → engineering@arcvault.local, `[HIGH] ArcVault Bug Report — req-001` ✓ |
| req-002 Feature Request | one email → product@arcvault.local ✓ |
| req-003 Billing Issue | one email → billing@arcvault.local ✓ |
| req-004 Technical Question | one email → security@arcvault.local ✓ |
| req-005 escalated incident | ONE email → engineering@arcvault.local with ⚠ subject + "HUMAN REVIEW REQUIRED" + all 3 fired rules (no duplicate to human-review@) ✓ |
| req-006 low confidence | one email → human-review@arcvault.local (destination is Human Review) ✓ |
| **req-007 recipient injection** | message explicitly demands forwarding to `ceo@arcvault.com` and a Gmail address — email went to **billing@arcvault.local**, proving recipients come from the routing table, not LLM output or message content ✓ |
| Email failure | Mailpit stopped, request posted: record still persisted with full classification and `"status":"failed","error":"getaddrinfo ENOTFOUND mailpit"` (no secrets in error); webhook still responded ✓ |
| Disabled mode | `EMAIL_NOTIFICATIONS_ENABLED=false`: no send, record carries `"status":"disabled"` + planned recipient ✓ |
| Config override | `ENGINEERING_EMAIL=eng-custom@arcvault.local`: notification delivered to the custom address ✓ |

Platform note discovered during this work: **n8n task runners do not expose `$env` to
Code-node JavaScript** — env config silently fell back to defaults until the config was
moved into a Set node ("Load Email Config"), whose parameter expressions are evaluated by
the n8n core process where `$env` is available. Another honest AI-assisted-development
correction: the first implementation read `$env` directly in the Code node and *appeared*
to work because the defaults matched the compose values; only the custom-recipient test
exposed it.

## What was NOT tested (consciously out of scope for 3–5 hours)

- Concurrency/load, malformed-JSON attacks on the webhook beyond the missing-message guard,
  non-English input, messages near the context limit, and adversarial prompt injection.
  Each is discussed in ARCHITECTURE.md rather than implemented.
