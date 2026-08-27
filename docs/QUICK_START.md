# Quick Start

Everything runs locally: the workflow engine (n8n) and email capture (Mailpit) in Docker,
and the LLM (Ollama) on your machine. **No accounts, API keys, or cloud services are
needed.**

## Prerequisites (all platforms)

| Tool | Why | Where |
|---|---|---|
| Docker Desktop (or Docker Engine + Compose on Linux) | runs n8n and Mailpit | docker.com |
| Ollama | runs the LLM locally | ollama.com/download — official installer for your OS |
| Git | clone the repository | git-scm.com |

Optional:
- **jq** — only used by the optional shell reporting scripts (`tests/run_tests.sh`,
  `tests/check_inbox.sh`). The core workflow never needs it, and Windows users use
  `tests/run_tests.ps1`, which needs nothing extra.
- **Python 3** — only for the bonus demo board (`ui/server.py`).

## 1. Get the code and the model

```bash
git clone https://github.com/patrickbeaino/arcvault-ai-triage.git
cd arcvault-ai-triage
ollama pull llama3.1:8b        # ~4.9 GB, one time
```

## 2. Start Ollama

- **Windows**: launch the Ollama application — the server starts automatically (tray
  icon). Do **not** also run `ollama serve`; it will report the address is already in use.
- **macOS**: launch the Ollama application, *or* if you installed the standalone CLI, run
  `ollama serve` in a terminal and leave it running.
- **Linux**: run `OLLAMA_HOST=0.0.0.0 ollama serve`. Binding to `0.0.0.0` is required on
  Linux so the n8n container can reach Ollama through Docker's host gateway.
  (macOS/Windows don't need this — Docker Desktop forwards `host.docker.internal` to
  localhost.)

Verify: open http://localhost:11434 — you should see "Ollama is running".

## 3. Start the stack

Same command on every platform (from the repository folder):

```bash
docker compose up -d
```

This starts **n8n** (http://localhost:5678) and the **Mailpit** email inbox
(http://localhost:8025).

## 4. Import and activate the workflow (first time only)

Same commands in Terminal (macOS/Linux) or PowerShell (Windows):

```bash
docker exec arcvault-n8n n8n import:workflow --input=/data/workflow/arcvault-intake.json
docker exec arcvault-n8n n8n publish:workflow --id=ArcVaultIntake01
docker compose restart n8n
```

## 5. Run the sample requests

- **macOS / Linux**: `./tests/run_tests.sh` (uses jq)
- **Windows (PowerShell)**: `.\tests\run_tests.ps1`

Or send a single request with no scripts at all:

macOS/Linux:
```bash
curl -X POST http://localhost:5678/webhook/arcvault-intake \
  -H 'Content-Type: application/json' -d @tests/requests/req-001.json
```

Windows PowerShell:
```powershell
Invoke-RestMethod -Uri http://localhost:5678/webhook/arcvault-intake -Method Post `
  -ContentType 'application/json' -InFile tests\requests\req-001.json
```

## What you should see

- Each request returns a **complete JSON record**: classification (category, priority,
  confidence), extracted identifiers, routing destination, escalation decision, and the
  email notification status.
- One file per request appears in `output/` (e.g. `output/req-001.json`).
- **http://localhost:5678** — n8n (create a local account on first visit, UI only). Open
  the workflow "ArcVault Intake & Triage" → **Executions** tab to replay any run node by
  node.
- **http://localhost:8025** — the Mailpit inbox holds one email per request, addressed to
  the team that owns the ticket, with ⚠ in the subject for escalated cases.

## How to verify the system is working

| Check | Expected |
|---|---|
| http://localhost:11434 | "Ollama is running" |
| http://localhost:5678/healthz | `{"status":"ok"}` |
| POST a sample request (step 5) | JSON response, `classification.category` = "Bug Report" for req-001 |
| `output/` folder | one `.json` file per request sent |
| http://localhost:8025 | one captured email per request |

The **first** request after starting Ollama takes ~10 seconds (the model loads into
memory); after that, ~3 seconds per request.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Webhook returns 404 | Workflow not imported/activated — rerun step 4 |
| Request hangs / LLM timeout | Ollama isn't running, or the model wasn't pulled (step 1–2) |
| Linux: "connection refused" to `host.docker.internal` | Start Ollama with `OLLAMA_HOST=0.0.0.0 ollama serve` |
| Port already in use (5678/8025) | Stop whatever occupies it, or change the port mapping in `docker-compose.yml` |
| Emails show `status: "failed"` | Mailpit container isn't running — `docker compose up -d` (the ticket record is still saved; that's by design) |
