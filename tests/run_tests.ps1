# Send every test request in tests/requests/ through the live n8n workflow
# and save the webhook responses to tests/responses/.
# Windows/PowerShell equivalent of run_tests.sh (works on PowerShell 5.1 and 7+).
# No jq required — PowerShell parses JSON natively.
$ErrorActionPreference = 'Stop'

$WebhookUrl = if ($env:WEBHOOK_URL) { $env:WEBHOOK_URL } else { 'http://localhost:5678/webhook/arcvault-intake' }
$Dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ResponsesDir = Join-Path $Dir 'responses'
New-Item -ItemType Directory -Force -Path $ResponsesDir | Out-Null

Get-ChildItem (Join-Path $Dir 'requests') -Filter '*.json' | Sort-Object Name | ForEach-Object {
    $name = $_.BaseName
    Write-Host "=== $name ===" -ForegroundColor Cyan
    $bodyBytes = [System.IO.File]::ReadAllBytes($_.FullName)   # send raw UTF-8 exactly as stored
    $r = Invoke-RestMethod -Uri $WebhookUrl -Method Post -ContentType 'application/json; charset=utf-8' `
        -Body $bodyBytes -TimeoutSec 180
    $r | ConvertTo-Json -Depth 12 | Set-Content -Encoding UTF8 (Join-Path $ResponsesDir "$name.response.json")
    [PSCustomObject]@{
        category    = $r.classification.category
        priority    = $r.classification.priority
        confidence  = $r.classification.confidence
        destination = $r.routing.destination
        escalated   = $r.escalation.flagged
        reason      = $r.escalation.reason
        email       = "$($r.notification.email.status) -> $($r.notification.email.recipient)"
    } | Format-List
}

Write-Host 'Responses saved to tests/responses/; persisted records in output/.'
