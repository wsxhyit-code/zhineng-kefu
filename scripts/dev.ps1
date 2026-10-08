# PowerShell launcher, the Windows stand-in for `make dev` / scripts/dev.sh.
# This machine has no make and no usable WSL distro, so dev.sh cannot run here.
#
#   pwsh -File scripts/dev.ps1              # containers + MCP servers + app
#   pwsh -File scripts/dev.ps1 -SkipDocker  # only MCP servers + app
#   pwsh -File scripts/dev-down.ps1         # stop MCP servers + app
#
# Logs go to log/, pids to data/ (both already gitignored).
# NOTE: this file is intentionally ASCII-only. A .ps1 containing non-ASCII text
# was mis-parsed by this shell earlier, so keep it ASCII.

param([switch]$SkipDocker)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# Python must read the Chinese markdown/jsonl in this repo as UTF-8.
# On a Chinese Windows the default is GBK and the knowledge base load throws.
$env:PYTHONUTF8 = '1'
$env:PYTHONPATH = '.'

# This machine runs a system proxy (Clash on 127.0.0.1:7890). Python's
# urllib.request.getproxies() reads it from the registry, and httpx -- used by
# langchain-mcp-adapters -- applies it to EVERY request without honouring the
# registry bypass list. Without this line the app cannot reach the two local
# MCP servers (:8101/:8102) and the agent never emits tool calls; it shows up
# only as "MCP Server unreachable ... TimeoutError" in the app log.
# Upstream model calls are not affected: only these hosts bypass the proxy.
$env:NO_PROXY = 'localhost,127.0.0.1,::1'
$env:no_proxy = 'localhost,127.0.0.1,::1'

# This checkout keeps uv's cache and its managed CPython 3.12 inside the project
# (the machine only has Python 3.14, for which asyncmy has no wheel).
# Remove these two lines if you install Python 3.12/3.13 system-wide.
if (-not $env:UV_CACHE_DIR)         { $env:UV_CACHE_DIR = Join-Path $root '.uv-cache' }
if (-not $env:UV_PYTHON_INSTALL_DIR) { $env:UV_PYTHON_INSTALL_DIR = Join-Path $root '.uv-python' }

if (-not (Test-Path '.env')) {
  Write-Error 'missing .env -- copy .env.example to .env and fill the three *_API_KEY values'
  exit 1
}

New-Item -ItemType Directory -Force -Path 'log', 'data' | Out-Null

$uv = (Get-Command uv).Source

if (-not $SkipDocker) {
  Write-Host 'starting dependency containers (mysql/etcd/minio/milvus)...'
  try {
    docker compose up -d mysql etcd minio milvus-standalone
  } catch {
    Write-Warning "docker compose failed: $($_.Exception.Message)"
    Write-Warning 'if Docker Desktop is not running, start it and re-run; continuing anyway'
  }
  Write-Host 'waiting for Milvus...'
  $ready = $false
  foreach ($i in 1..40) {
    $probe = & $uv run --no-sync python -c "import pymilvus,sys; from pymilvus import MilvusClient; MilvusClient(uri='http://localhost:19530').get_server_version()" 2>$null
    if ($LASTEXITCODE -eq 0) { $ready = $true; break }
    Start-Sleep -Seconds 3
  }
  if ($ready) { Write-Host 'Milvus OK' } else { Write-Warning 'Milvus not ready, continuing anyway' }
}

function Start-Svc {
  # NOTE: do not name this parameter $Args -- that collides with PowerShell's
  # automatic $Args variable and Start-Process then receives null.
  param([string]$Name, [string[]]$AppArgs)
  # app writes log/app.log itself via its own FileHandler, so keep the stdout and
  # stderr redirects on separate files instead of two writers on one file.
  $out = Join-Path $root "log\$Name.out.log"
  $err = Join-Path $root "log\$Name.err.log"
  $p = Start-Process -FilePath $uv -ArgumentList $AppArgs -WorkingDirectory $root -PassThru `
        -RedirectStandardOutput $out -RedirectStandardError $err -WindowStyle Hidden
  Set-Content -Path (Join-Path $root "data\$Name.pid") -Value $p.Id -Encoding ascii
  Write-Host ("  {0,-12} pid={1}  -> log\{0}.out.log" -f $Name, $p.Id)
}

Write-Host 'starting MCP servers (logistics :8101, aftersales :8102)...'
Start-Svc 'mcp-logistics'  @('run', '--no-sync', 'python', 'mcp_servers/logistics_server.py')
Start-Svc 'mcp-aftersales' @('run', '--no-sync', 'python', 'mcp_servers/aftersales_server.py')

Write-Host 'starting app on :8000...'
Start-Svc 'app' @('run', '--no-sync', 'uvicorn', 'app.main:app', '--port', '8000')

Start-Sleep -Seconds 6
Write-Host ''
Write-Host 'chat page -> http://localhost:8000'
Write-Host 'to stop   -> powershell -File scripts\dev-down.ps1'
