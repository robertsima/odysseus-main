# Creates the disposable ./zima-local tree that docker-compose.zimaos-local.yml
# binds, mirroring the ZimaOS /DATA layout, and seeds distinctive notes so the
# public/private split is actually observable in retrieval.
#
#   pwsh -File zima-local-setup.ps1
#   docker compose -f docker-compose.zimaos-local.yml up --build

$ErrorActionPreference = 'Stop'
$root = Join-Path $PSScriptRoot 'zima-local'

# AppData side: writable state. Must exist before compose starts, or Docker
# creates them root-owned and the entrypoint's chown has more to repair.
$appData = Join-Path $root 'AppData/odysseus'
$dirs = @(
    "$appData/data"
    "$appData/data/personal_docs"
    "$appData/data/chromadb"
    "$appData/data/searxng"
    "$appData/data/ntfy"
    "$appData/data/ssh"
    "$appData/data/huggingface"
    "$appData/data/local"
    "$appData/logs"
)

# Vault side: read-only content, same three trees as ZimaOS.
$vault = Join-Path $root 'Vault'
$dirs += @(
    "$vault/Vault Mind"
    "$vault/AI Mind"
    "$vault/Private/Journal"
)

foreach ($d in $dirs) {
    if (-not (Test-Path $d)) { New-Item -ItemType Directory -Path $d -Force | Out-Null }
}

# Seed notes. Each carries a unique token so you can tell from a chat reply
# which tree a hit came from, rather than guessing from paraphrase.
$seeds = @{
    "$vault/Vault Mind/architecture.md" = @'
# Architecture notes

The retrieval pipeline uses token PUBLICVAULT7788 as its canonical marker.
Public reference material: safe for any model, local or hosted.
'@
    "$vault/AI Mind/models.md" = @'
# Model notes

Working notes on local serving. Marker token AIMIND4413.
'@
    "$vault/Private/Journal/2026-08-01.md" = @'
# Journal entry

Private reflection. Marker token PRIVATEJOURNAL9021.
This must never be retrieved for a session served by a hosted API endpoint.
'@
}

foreach ($path in $seeds.Keys) {
    if (-not (Test-Path $path)) {
        Set-Content -Path $path -Value $seeds[$path] -Encoding utf8
    }
}

# An .obsidian/ directory to confirm it is pruned rather than indexed (#5559).
$obsidian = Join-Path $vault 'Vault Mind/.obsidian'
if (-not (Test-Path $obsidian)) { New-Item -ItemType Directory -Path $obsidian -Force | Out-Null }
Set-Content -Path (Join-Path $obsidian 'workspace.json') -Value '{"marker":"SHOULDNOTBEINDEXED"}' -Encoding utf8

Write-Output "Created $root"
Write-Output ""
Write-Output "Next:"
Write-Output "  docker compose -f docker-compose.zimaos-local.yml up --build"
Write-Output ""
Write-Output "Then label the trees (needs an admin session cookie or API token):"
Write-Output '  POST /api/personal/add_directory {"directory":"Vault Mind","sensitivity":"public"}'
Write-Output '  POST /api/personal/add_directory {"directory":"AI Mind","sensitivity":"public"}'
Write-Output '  POST /api/personal/add_directory {"directory":"Journal","sensitivity":"private"}'
Write-Output ""
Write-Output "Verify:"
Write-Output "  - ask about PRIVATEJOURNAL9021 with a host.docker.internal model -> should hit"
Write-Output "  - ask the same with an API model            -> should NOT hit"
Write-Output "  - PUBLICVAULT7788 should hit in both cases"
Write-Output "  - SHOULDNOTBEINDEXED should never hit (.obsidian is pruned)"
