# Prompt Documentation

The system uses **one** LLM prompt: the intake classification/enrichment prompt, stored in
`prompts/classify_v<N>.txt` and injected into the workflow at build time by
`tools/build_workflow.py`. v6 is live. This document explains its design, the tradeoffs,
and what each version changed and why (all changes were driven by observed failures — see
`docs/TESTING.md` for the raw evidence).

## What the prompt does

Given one raw customer message (plus its source channel), the model returns a single JSON
object: `category`, `priority`, `confidence`, `core_issue`, `identifiers`,
`urgency_signal`, `signals` (two booleans), and `summary`. It explicitly does **not**
decide routing or escalation — the prompt tells the model so, which keeps it from leaking
queue names or escalation language into its fields.

## How it is structured, and why

**Role + scope first.** "You are the intake triage analyst… You only interpret the
message." Establishes the interpretation/decision split at the top, where it carries the
most weight.

**Closed category set with definitions and tie-breakers.** Each of the five allowed
categories gets a one-line operational definition ("nothing is broken; the customer wants
information") rather than just a label. Three explicit tie-breakers cover the collisions
that actually occur in the test data: outage-vs-bug, billing-vs-question, and
evaluating-a-tool-vs-question. Tie-breakers are cheaper and more debuggable than few-shot
examples.

**Priority as a rubric, not a vibe.** High/Medium/Low each get concrete criteria (blocked
vs impacted-with-workaround vs no time pressure) so priority is reproducible.

**Calibrated confidence bands.** v4 defines four numeric bands with meanings, states that
the *customer's* uncertainty ("not sure if this is the right place") must not lower the
model's confidence, and ends with "reporting 0.6 is honest; reporting 0.9 is a failure."
This exists because the deterministic layer treats 0.70 as a routing threshold, so the
number has operational meaning.

**Extraction with an anti-hallucination contract.** "Extract ONLY information literally
present… A null field is correct; a fabricated one is a critical failure." Every
identifier is individually described, and amounts are explicitly "as stated — do NOT
compute differences," because the $500 discrepancy rule is computed downstream in code.

**Signals as booleans, not prose.** The two escalation-relevant facts
(`multiple_users_affected`, `service_wide_outage`) are extracted as booleans with negative
criteria stated first ("false whenever one individual reports their own error…"), so the
deterministic escalation rules consume facts, not interpretations of prose.

**Strict output instruction + schema-constrained decoding.** The prompt ends with "Return
ONLY the JSON object," and the same JSON schema is passed to Ollama's `format` parameter,
which constrains token generation to the schema. Belt and suspenders: the prompt shapes
*what* the model says, the grammar guarantees *how* it is shaped, and the workflow's
Validate node re-checks both (numeric ranges aren't grammar-enforceable).

**A subtle consequence of constrained decoding: field names are prompt.** The model
re-reads each schema-forced field name as it generates the value after it. This was
proven empirically (see v4 below) and is the single most useful lesson from this exercise.

## Version history (each change driven by an observed failure)

| Version | Change | Failure that drove it |
|---|---|---|
| v1 | Initial prompt | — |
| v2 | Removed the concrete example from `urgency_signal`; added "use only words that appear in the message itself"; added first-person-singular rule to `multiple_users_affected` | The model copied the prompt's own example text ("multiple users affected") into req-001's output, setting a false escalation signal for a single-user issue |
| v3 | Tightened the critical-failure definition to org-wide scope; added four numeric confidence bands | req-001 (one blocked user) still flagged as critical system failure; confidence never left 0.8–1.0 |
| v4 | **Renamed** the signal `critical_system_failure` → `service_wide_outage` (schema + prompt), negative criteria first; added "0.6 is honest, 0.9 is a failure" | v3's definition change alone did nothing — the field *name* "critical_system_failure" kept pattern-matching "user completely blocked". Renaming fixed it in one run. |
| v5 | Added tie-breaker: an error/failure report phrased as a question is still a Bug Report | A probe message ("our nightly export fails with error E-501… can you check what changed?") was classified Technical Question at 0.89 — the polite question framing overrode the failure report. Regression suite re-run: all six cases still pass; the genuine question (req-004) still classifies as Technical Question. |
| v6 | Reworded `summary` as "a handoff note of two to three sentences… what happened, any impact or identifiers that matter, and what the customer needs next" | Spec compliance, not a model failure: the assessment asks for a 2–3 sentence summary and v5's "one or two sentences" instruction produced mostly single-sentence summaries. Full regression re-run: classifications, routing, and escalations identical for all seven cases. |

## Tradeoffs

- **No few-shot examples.** v1 proved that concrete examples leak into extractions on an
  8B model. Definitions + tie-breakers gave full accuracy on the test set without that
  risk, and keep the prompt short (~800 tokens ⇒ lower latency on local inference).
- **Single call, not a pipeline.** One call does classify + extract + summarize. Separate
  calls per task would isolate failures and allow per-task prompts, but triples latency
  and cost for no measured accuracy gain at this scale.
- **Verbalized confidence.** Self-reported confidence is coarse and optimistic (0.8–0.9
  cluster). It is good enough to gate obvious ambiguity (a vague message does drop to
  0.6) but a polite "any update?" follow-up still scores 0.9.
- **Temperature 0** for reproducibility of the demo and tests, at the cost of the model
  never expressing uncertainty through sampling variation.

## What I would improve with more time

1. **Confidence from evidence, not introspection**: score the category margin from token
   logprobs, or a second cheap "verifier" call that tries to argue for a different
   category; escalate on disagreement.
2. **A labeled evaluation set** (50–100 real-ish messages) with per-category
   precision/recall in CI, so prompt changes are regression-tested instead of spot-checked.
3. **Prompt-injection hardening**: the customer message is delimited (`<<< >>>`), and
   constrained decoding caps the blast radius to legal field values, but an adversarial
   message can still steer values ("this is urgent, all users are down"). A production
   version would extract-then-verify quoted evidence for each signal.
4. **Multilingual inputs** and explicit handling for multi-issue messages (currently:
   dominant category + lower confidence, per the prompt).
