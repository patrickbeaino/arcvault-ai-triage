#!/usr/bin/env bash
# Send every test request in tests/requests/ through the live n8n workflow
# and save the webhook responses to tests/responses/.
# Persisted records (written by the workflow itself) land in output/.
set -euo pipefail

WEBHOOK_URL="${WEBHOOK_URL:-http://localhost:5678/webhook/arcvault-intake}"
DIR="$(cd "$(dirname "$0")" && pwd)"

for req in "$DIR"/requests/*.json; do
  name="$(basename "$req" .json)"
  echo "=== $name ==="
  curl -sS --max-time 180 -X POST "$WEBHOOK_URL" \
    -H 'Content-Type: application/json' \
    -d @"$req" | jq . | tee "$DIR/responses/$name.response.json" \
    | jq '{category: .classification.category, priority: .classification.priority, confidence: .classification.confidence, destination: .routing.destination, escalated: .escalation.flagged, reason: .escalation.reason, email: ((.notification.email.status // "n/a") + " → " + (.notification.email.recipient // "-"))}'
  echo
done

echo "Responses saved to tests/responses/; persisted records in output/."
