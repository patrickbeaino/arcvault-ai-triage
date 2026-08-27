#!/usr/bin/env bash
# List every captured notification in the local Mailpit inbox (recipient + subject).
set -euo pipefail
MAILPIT="${MAILPIT_URL:-http://localhost:8025}"
curl -s "$MAILPIT/api/v1/messages?limit=100" \
  | jq -r '.messages[] | [.To[0].Address, .Subject] | @tsv' \
  | column -t -s$'\t'
echo
echo "Inbox UI: $MAILPIT"
