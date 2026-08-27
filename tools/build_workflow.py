#!/usr/bin/env python3
"""Build the n8n workflow JSON for the ArcVault intake & triage assessment.

The system prompt lives in prompts/classify_v<N>.txt and is injected into the
workflow at build time, so the prompt can be versioned and edited as plain text
instead of hand-editing escaped strings inside workflow JSON.

Usage: python3 tools/build_workflow.py [prompt_version]   (default: latest vN found)
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = ROOT / "prompts"
OUT_PATH = ROOT / "workflow" / "arcvault-intake.json"

MODEL = "llama3.1:8b"
OLLAMA_URL = "http://host.docker.internal:11434/api/chat"  # n8n runs in Docker; Ollama runs on the host
WEBHOOK_PATH = "arcvault-intake"
WORKFLOW_ID = "ArcVaultIntake01"  # fixed id so CLI import/publish are repeatable

ALLOWED_CATEGORIES = [
    "Bug Report", "Feature Request", "Billing Issue",
    "Technical Question", "Incident/Outage",
]
ALLOWED_PRIORITIES = ["Low", "Medium", "High"]

# JSON schema passed to Ollama's `format` parameter. Ollama constrains decoding
# to this schema, so the model cannot emit malformed JSON or out-of-enum values.
# (Numeric min/max are NOT grammar-enforced, so the Validate node re-checks them.)
LLM_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "enum": ALLOWED_CATEGORIES},
        "priority": {"type": "string", "enum": ALLOWED_PRIORITIES},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "core_issue": {"type": "string"},
        "identifiers": {
            "type": "object",
            "properties": {
                "account_id": {"type": ["string", "null"]},
                "invoice_number": {"type": ["string", "null"]},
                "error_code": {"type": ["string", "null"]},
                "amount_charged_usd": {"type": ["number", "null"]},
                "amount_expected_usd": {"type": ["number", "null"]},
                "external_systems": {"type": "array", "items": {"type": "string"}},
            },
            "required": [
                "account_id", "invoice_number", "error_code",
                "amount_charged_usd", "amount_expected_usd", "external_systems",
            ],
        },
        "urgency_signal": {"type": "string"},
        "signals": {
            "type": "object",
            "properties": {
                "multiple_users_affected": {"type": "boolean"},
                "service_wide_outage": {"type": "boolean"},
            },
            "required": ["multiple_users_affected", "service_wide_outage"],
        },
        "summary": {"type": "string"},
    },
    "required": [
        "category", "priority", "confidence", "core_issue",
        "identifiers", "urgency_signal", "signals", "summary",
    ],
}

# ---------------------------------------------------------------------------
# Code node sources (plain JS, run once per item)
# ---------------------------------------------------------------------------

NORMALIZE_INPUT_JS = r"""
// Normalize the raw webhook payload into a consistent internal shape.
const body = $json.body || {};

const rawMessage = (body.message || body.raw_message || body.text || '').toString().trim();
if (!rawMessage) {
  throw new Error('Request rejected: no message text found in payload (expected "message" field).');
}

// Normalize source into a small known set; keep the original for the record.
const sourceRaw = (body.source || 'unknown').toString().trim().toLowerCase().replace(/[\s-]+/g, '_');
const SOURCE_ALIASES = { email: 'email', web_form: 'web_form', webform: 'web_form', form: 'web_form', support_portal: 'support_portal', portal: 'support_portal' };
const source = SOURCE_ALIASES[sourceRaw] || 'unknown';

// Use the caller's request id if provided, otherwise generate one.
const requestId = (body.request_id || `req-${Date.now()}`).toString();

return {
  request_id: requestId,
  source,
  raw_message: rawMessage,
  received_at: new Date().toISOString(),
  llm_user_message: `Source channel: ${source}\nCustomer message:\n<<<\n${rawMessage}\n>>>`,
};
""".strip()

VALIDATE_JS = r"""
// Never trust LLM output blindly: parse it and validate every field we rely on.
// On failure we do NOT throw — we mark validation as failed so deterministic
// logic downstream routes the request to Human Review instead of dropping it.
const ALLOWED_CATEGORIES = __CATEGORIES__;
const ALLOWED_PRIORITIES = __PRIORITIES__;

const norm = $('Normalize Input').item.json;
const errors = [];
let analysis = null;

try {
  const content = $json.message && $json.message.content;
  if (typeof content !== 'string' || !content.trim()) throw new Error('empty LLM response');
  analysis = JSON.parse(content);
} catch (e) {
  errors.push(`LLM response is not valid JSON: ${e.message}`);
}

if (analysis) {
  if (!ALLOWED_CATEGORIES.includes(analysis.category)) errors.push(`category "${analysis.category}" is not an allowed category`);
  if (!ALLOWED_PRIORITIES.includes(analysis.priority)) errors.push(`priority "${analysis.priority}" is not an allowed priority`);
  if (typeof analysis.confidence !== 'number' || Number.isNaN(analysis.confidence) || analysis.confidence < 0 || analysis.confidence > 1) {
    errors.push(`confidence "${analysis.confidence}" is not a number between 0 and 1`);
  }
  if (typeof analysis.core_issue !== 'string' || !analysis.core_issue.trim()) errors.push('core_issue is missing or empty');
  if (typeof analysis.summary !== 'string' || !analysis.summary.trim()) errors.push('summary is missing or empty');
  if (typeof analysis.urgency_signal !== 'string') errors.push('urgency_signal is missing');
  if (!analysis.identifiers || typeof analysis.identifiers !== 'object') {
    errors.push('identifiers object is missing');
  } else {
    // Ensure every expected identifier key exists; null is the correct "absent" value.
    const ids = analysis.identifiers;
    for (const k of ['account_id', 'invoice_number', 'error_code']) {
      if (ids[k] !== null && typeof ids[k] !== 'string') ids[k] = ids[k] == null ? null : String(ids[k]);
    }
    for (const k of ['amount_charged_usd', 'amount_expected_usd']) {
      if (ids[k] !== null && typeof ids[k] !== 'number') errors.push(`${k} must be a number or null`);
    }
    if (!Array.isArray(ids.external_systems)) ids.external_systems = [];
  }
  if (!analysis.signals || typeof analysis.signals.multiple_users_affected !== 'boolean' || typeof analysis.signals.service_wide_outage !== 'boolean') {
    errors.push('signals booleans are missing');
  }
}

return {
  request_id: norm.request_id,
  source: norm.source,
  raw_message: norm.raw_message,
  received_at: norm.received_at,
  analysis,
  validation: { passed: errors.length === 0, errors },
};
""".strip()

ROUTE_JS = r"""
// Deterministic routing. The LLM classifies; this table decides the queue.
const ROUTING_TABLE = {
  'Bug Report': 'Engineering',
  'Feature Request': 'Product',
  'Billing Issue': 'Billing',
  'Technical Question': 'IT/Security',
  'Incident/Outage': 'Engineering',
};
const CONFIDENCE_THRESHOLD = 0.70;

let destination;
let rule;

if (!$json.validation.passed) {
  destination = 'Human Review / Escalation';
  rule = 'llm_output_failed_validation';
} else if ($json.analysis.confidence < CONFIDENCE_THRESHOLD) {
  destination = 'Human Review / Escalation';
  rule = 'confidence_below_0.70';
} else {
  destination = ROUTING_TABLE[$json.analysis.category];
  rule = 'category_routing_table';
}

return { ...$json, routing: { destination, rule } };
""".strip()

ESCALATION_JS = r"""
// Deterministic escalation rules from the assessment. Each rule appends a
// human-readable reason; the flag is simply "any rule fired".
const CONFIDENCE_THRESHOLD = 0.70;
const BILLING_DISCREPANCY_THRESHOLD_USD = 500;

const a = $json.analysis;
const reasons = [];
let billingDiscrepancy = null;

if (!$json.validation.passed) {
  reasons.push(`LLM output failed validation (${$json.validation.errors.join('; ')})`);
}

if (a && $json.validation.passed) {
  // Rule 1: low classification confidence
  if (a.confidence < CONFIDENCE_THRESHOLD) {
    reasons.push(`Classification confidence ${a.confidence.toFixed(2)} is below the ${CONFIDENCE_THRESHOLD} threshold`);
  }
  // Rule 2: the request describes an outage/incident
  if (a.category === 'Incident/Outage') {
    reasons.push('Request describes an incident or outage');
  }
  // Rule 3: multiple users affected
  if (a.signals.multiple_users_affected) {
    reasons.push('Multiple users are affected');
  }
  // Rule 4: explicit critical system failure (extracted as service-wide outage
  // so a single blocked user does not qualify)
  if (a.signals.service_wide_outage) {
    reasons.push('Message indicates a critical, service-wide system failure');
  }
  // Rule 5: billing discrepancy > $500 — computed in code from the two
  // extracted amounts; we never ask the LLM to do arithmetic.
  const charged = a.identifiers.amount_charged_usd;
  const expected = a.identifiers.amount_expected_usd;
  if (typeof charged === 'number' && typeof expected === 'number') {
    billingDiscrepancy = Math.round(Math.abs(charged - expected) * 100) / 100;
    if (billingDiscrepancy > BILLING_DISCREPANCY_THRESHOLD_USD) {
      reasons.push(`Billing discrepancy $${billingDiscrepancy} exceeds $${BILLING_DISCREPANCY_THRESHOLD_USD}`);
    }
  }
}

return {
  ...$json,
  billing_discrepancy_usd: billingDiscrepancy,
  escalation: {
    flagged: reasons.length > 0,
    reason: reasons.length > 0 ? reasons.join(' | ') : null,
  },
};
""".strip()

# Evaluated by the n8n MAIN process (Set-node expression), where $env is
# available — task runners do not expose env vars to Code-node JS, so all
# env-driven configuration is injected into the item here.
EMAIL_CONFIG_EXPR = (
    "={{ { "
    "enabled: ($env.EMAIL_NOTIFICATIONS_ENABLED ?? 'true').toString().toLowerCase() !== 'false', "
    "mailpit_url: $env.MAILPIT_API_URL || 'http://mailpit:8025', "
    "recipients: { "
    "'Engineering': $env.ENGINEERING_EMAIL || 'engineering@arcvault.local', "
    "'Product': $env.PRODUCT_EMAIL || 'product@arcvault.local', "
    "'Billing': $env.BILLING_EMAIL || 'billing@arcvault.local', "
    "'IT/Security': $env.SECURITY_EMAIL || 'security@arcvault.local', "
    "'Human Review / Escalation': $env.HUMAN_REVIEW_EMAIL || 'human-review@arcvault.local' "
    "} } }}"
)

COMPOSE_NOTIFICATION_JS = r"""
// Deterministic notification planning. Recipients come ONLY from the routing
// destination via the config map injected by Load Email Config — the LLM
// output contains no recipient fields at all, so it cannot influence who
// gets notified.
const { notification_config: cfg, ...rest } = $json;
const enabled = cfg.enabled;
const RECIPIENTS = cfg.recipients;

const a = $json.analysis;
const dest = $json.routing.destination;
const recipient = RECIPIENTS[dest] || RECIPIENTS['Human Review / Escalation'];
const flagged = $json.escalation.flagged;
const prio = a && a.priority ? a.priority.toUpperCase() : 'UNTRIAGED';
const cat = a && a.category ? a.category : 'Unclassified request';
const subject = `[${prio}]${flagged ? ' ⚠' : ''} ArcVault ${cat} — ${$json.request_id}`;

const idLines = a
  ? Object.entries(a.identifiers)
      .filter(([, v]) => v !== null && !(Array.isArray(v) && v.length === 0))
      .map(([k, v]) => `  ${k}: ${Array.isArray(v) ? v.join(', ') : v}`)
  : [];
if ($json.billing_discrepancy_usd != null) {
  idLines.push(`  billing_discrepancy_usd (computed): $${$json.billing_discrepancy_usd}`);
}

const body = [
  'ArcVault AI Triage Notification',
  '',
  `Request ID: ${$json.request_id}`,
  `Source: ${$json.source}`,
  `Received: ${$json.received_at}`,
  '',
  'Classification',
  `  Category: ${cat}`,
  `  Priority: ${a ? a.priority : 'n/a'}`,
  `  Confidence: ${a ? Math.round(a.confidence * 100) + '%' : 'n/a'}`,
  '',
  'Issue',
  `  ${a ? a.core_issue : 'LLM output failed validation - manual triage required.'}`,
  ...(idLines.length ? ['', 'Extracted identifiers', ...idLines] : []),
  '',
  `Urgency signal: ${a ? a.urgency_signal : 'n/a'}`,
  '',
  'Routing',
  `  Destination: ${dest}`,
  `  Rule: ${$json.routing.rule}`,
  '',
  'Escalation',
  flagged ? '  ⚠ HUMAN REVIEW REQUIRED' : '  Not escalated',
  ...(flagged ? [`  Reason: ${$json.escalation.reason}`] : []),
  '',
  'Summary',
  `  ${a ? a.summary : $json.raw_message.slice(0, 200)}`,
  '',
  'Original request',
  `  "${$json.raw_message}"`,
].join('\n');

return { ...rest, notification_plan: {
  enabled, recipient, recipient_team: dest, subject, body, mailpit_url: cfg.mailpit_url } };
""".strip()

SEND_EMAIL_JS = r"""
// Delivery + status capture. An email failure must never destroy the ticket:
// every error is caught and recorded, and the workflow continues to
// persistence with notification.email.status = "failed".
const plan = $json.notification_plan;
const { notification_plan, ...rest } = $json;
const base = { enabled: plan.enabled, recipient: plan.recipient, recipient_team: plan.recipient_team };

if (!plan.enabled) {
  return { ...rest, notification: { email: { ...base, status: 'disabled' } } };
}

try {
  const res = await this.helpers.httpRequest({
    method: 'POST',
    url: (plan.mailpit_url || 'http://mailpit:8025') + '/api/v1/send',
    body: {
      From: { Email: 'triage@arcvault.local', Name: 'ArcVault AI Triage' },
      To: [{ Email: plan.recipient }],
      Subject: plan.subject,
      Text: plan.body,
    },
    json: true,
    timeout: 5000,
  });
  return { ...rest, notification: { email: {
    ...base, status: 'sent', subject: plan.subject, message_id: (res && res.ID) || null } } };
} catch (e) {
  return { ...rest, notification: { email: {
    ...base, status: 'failed', error: String((e && e.message) || e).slice(0, 200) } } };
}
""".strip()

BUILD_RECORD_JS_TEMPLATE = r"""
// Assemble the final structured record and attach it as binary so the next
// node can persist it as a standalone JSON file.
const item = items[0].json;
const a = item.analysis;

const record = {
  request_id: item.request_id,
  source: item.source,
  received_at: item.received_at,
  raw_message: item.raw_message,
  classification: {
    category: a ? a.category : null,
    priority: a ? a.priority : null,
    confidence: a ? a.confidence : null,
  },
  enrichment: {
    core_issue: a ? a.core_issue : null,
    identifiers: a ? a.identifiers : {},
    billing_discrepancy_usd: item.billing_discrepancy_usd,
    urgency_signal: a ? a.urgency_signal : null,
    signals: a ? a.signals : null,
  },
  routing: { destination: item.routing.destination, rule: item.routing.rule },
  escalation: { flagged: item.escalation.flagged, reason: item.escalation.reason },
  notification: item.notification || { email: { enabled: false, status: 'not_run' } },
  summary: a ? a.summary : null,
  meta: {
    llm_model: '__MODEL__',
    prompt_version: '__PROMPT_VERSION__',
    validation: item.validation,
    processed_at: new Date().toISOString(),
  },
};

const fileName = `${record.request_id}.json`;
return [{
  json: record,
  binary: {
    data: {
      data: Buffer.from(JSON.stringify(record, null, 2)).toString('base64'),
      mimeType: 'application/json',
      fileName,
    },
  },
}];
""".strip()


def code_node(name, js, x, y, mode="runOnceForEachItem", node_id=None):
    params = {"jsCode": js}
    if mode == "runOnceForEachItem":
        params["mode"] = "runOnceForEachItem"
    return {
        "id": node_id or name.lower().replace(" ", "-").replace("/", "-"),
        "name": name,
        "type": "n8n-nodes-base.code",
        "typeVersion": 2,
        "position": [x, y],
        "parameters": params,
    }


def build(prompt_version: str) -> dict:
    prompt_path = PROMPTS_DIR / f"classify_{prompt_version}.txt"
    system_prompt = prompt_path.read_text()

    # jsonBody is an n8n template string ("=" prefix). Static JSON is authored
    # here with proper escaping; only the user message is a runtime expression.
    llm_body = (
        "={\n"
        f'  "model": {json.dumps(MODEL)},\n'
        '  "stream": false,\n'
        '  "keep_alive": "30m",\n'
        '  "options": { "temperature": 0, "num_ctx": 4096 },\n'
        f'  "format": {json.dumps(LLM_OUTPUT_SCHEMA)},\n'
        '  "messages": [\n'
        f'    {{ "role": "system", "content": {json.dumps(system_prompt)} }},\n'
        '    { "role": "user", "content": {{ JSON.stringify($json.llm_user_message) }} }\n'
        '  ]\n'
        "}"
    )

    validate_js = VALIDATE_JS.replace("__CATEGORIES__", json.dumps(ALLOWED_CATEGORIES)).replace(
        "__PRIORITIES__", json.dumps(ALLOWED_PRIORITIES))
    build_record_js = BUILD_RECORD_JS_TEMPLATE.replace("__MODEL__", MODEL).replace(
        "__PROMPT_VERSION__", prompt_version)

    y = 300
    nodes = [
        {
            "id": "receive-request",
            "name": "Receive Request",
            "type": "n8n-nodes-base.webhook",
            "typeVersion": 2,
            "position": [-600, y],
            "webhookId": "7f9c2a1e-3b5d-4e8f-9a6c-1d2e3f4a5b6c",
            "parameters": {
                "httpMethod": "POST",
                "path": WEBHOOK_PATH,
                "responseMode": "responseNode",
                "options": {},
            },
        },
        code_node("Normalize Input", NORMALIZE_INPUT_JS, -380, y),
        {
            "id": "analyze-with-llm",
            "name": "Analyze with LLM",
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.2,
            "position": [-160, y],
            "parameters": {
                "method": "POST",
                "url": OLLAMA_URL,
                "sendBody": True,
                "specifyBody": "json",
                "jsonBody": llm_body,
                "options": {"timeout": 120000},
            },
        },
        code_node("Validate LLM Output", validate_js, 60, y),
        code_node("Determine Route", ROUTE_JS, 280, y),
        code_node("Determine Escalation", ESCALATION_JS, 500, y),
        {
            "id": "load-email-config",
            "name": "Load Email Config",
            "type": "n8n-nodes-base.set",
            "typeVersion": 3.4,
            "position": [720, y],
            "parameters": {
                "mode": "manual",
                "includeOtherFields": True,
                "assignments": {
                    "assignments": [
                        {
                            "id": "email-config-1",
                            "name": "notification_config",
                            "type": "object",
                            "value": EMAIL_CONFIG_EXPR,
                        }
                    ]
                },
                "options": {},
            },
        },
        code_node("Compose Notification", COMPOSE_NOTIFICATION_JS, 940, y),
        code_node("Send Email Notification", SEND_EMAIL_JS, 1160, y),
        code_node("Build Final Record", build_record_js, 1380, y, mode="runOnceForAllItems"),
        {
            "id": "persist-output",
            "name": "Persist Output",
            "type": "n8n-nodes-base.readWriteFile",
            "typeVersion": 1,
            "position": [1600, y],
            "parameters": {
                "operation": "write",
                "fileName": "=/data/output/{{ $binary.data.fileName }}",
                "dataPropertyName": "data",
                "options": {},
            },
        },
        {
            "id": "respond-to-webhook",
            "name": "Respond to Webhook",
            "type": "n8n-nodes-base.respondToWebhook",
            "typeVersion": 1.1,
            "position": [1820, y],
            "parameters": {
                "respondWith": "json",
                "responseBody": "={{ $('Build Final Record').item.json }}",
                "options": {},
            },
        },
    ]

    order = [n["name"] for n in nodes]
    connections = {
        a: {"main": [[{"node": b, "type": "main", "index": 0}]]}
        for a, b in zip(order, order[1:])
    }

    return {
        "id": WORKFLOW_ID,
        "name": "ArcVault Intake & Triage",
        "nodes": nodes,
        "connections": connections,
        "settings": {"executionOrder": "v1"},
        "active": True,
    }


def latest_prompt_version() -> str:
    versions = sorted(
        int(m.group(1))
        for p in PROMPTS_DIR.glob("classify_v*.txt")
        if (m := re.match(r"classify_v(\d+)\.txt", p.name))
    )
    if not versions:
        sys.exit("No prompts/classify_v*.txt found")
    return f"v{versions[-1]}"


if __name__ == "__main__":
    version = sys.argv[1] if len(sys.argv) > 1 else latest_prompt_version()
    wf = build(version)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(wf, indent=2) + "\n")
    print(f"Wrote {OUT_PATH} (prompt {version}, model {MODEL})")
