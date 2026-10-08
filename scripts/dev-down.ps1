# Stop what scripts/dev.ps1 started (the Windows stand-in for `make dev-down`).
# Containers are left running; stop them with `docker compose stop`.
# ASCII-only on purpose (see the note in dev.ps1).

$root = Split-Path -Parent $PSScriptRoot

function Get-Owner([int]$procId) {
  $p = Get-Process -Id $procId -ErrorAction SilentlyContinue
  if (-not $p) { return 'gone' }
  # A pid file can be stale (reboot, crash, reaped child). Only ever kill a
  # process that is plausibly ours -- the uv launcher, or a python under this
  # project -- so a recycled PID can never take down somebody else's process.
  $path = ''
  try { $path = $p.Path } catch { }
  if (-not $path) { return 'unknown' }
  if ($path -like '*\uv.exe') { return 'ours' }
  if ($path -like "$root\.venv\*" -or $path -like "$root\.uv-python\*") { return 'ours' }
  return 'foreign'
}

foreach ($name in 'app', 'mcp-logistics', 'mcp-aftersales') {
  $pidFile = Join-Path $root "data\$name.pid"
  if (-not (Test-Path $pidFile)) { Write-Host "  $name : no pid file"; continue }

  $procId = 0
  [void][int]::TryParse((Get-Content $pidFile -Raw).Trim(), [ref]$procId)
  if ($procId -le 0) { Remove-Item $pidFile -Force; Write-Host "  $name : pid file unreadable, removed"; continue }

  switch (Get-Owner $procId) {
    'ours' {
      # uv spawns python as a child, so kill the whole tree, not just the launcher.
      & taskkill /PID $procId /T /F 2>&1 | Out-Null
      # Do not claim success we did not get: verify the process is actually gone.
      if (Get-Process -Id $procId -ErrorAction SilentlyContinue) {
        Write-Host "  $name : STILL RUNNING (pid $procId) -- taskkill did not take effect."
        Write-Host "         fallback: Get-Process python,uv -ErrorAction SilentlyContinue |"
        Write-Host "                   Where-Object { `$_.Path -like '$root\*' } | Stop-Process -Force"
      } else {
        Write-Host "  $name : stopped (pid $procId)"
      }
    }
    'gone'  { Write-Host "  $name : not running (pid $procId already exited)" }
    default { Write-Host "  $name : pid $procId is not ours, left alone (stale pid file removed)" }
  }
  Remove-Item $pidFile -Force
}

Write-Host 'done. containers still up: docker compose stop'
