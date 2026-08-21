Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
function Test-PythonLauncher([string]$Source, [string[]]$Arguments) {
  try {
    & $Source @Arguments --version 2>$null | Out-Null
    return $LASTEXITCODE -eq 0
  } catch {
    return $false
  }
}

function Find-Python {
  $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
  if ($pyLauncher -and (Test-PythonLauncher $pyLauncher.Source @('-3'))) {
      return @{
        Source = $pyLauncher.Source
        Arguments = @('-3')
      }
  }
  $pythonLauncher = Get-Command python -ErrorAction SilentlyContinue
  if ($pythonLauncher -and (Test-PythonLauncher $pythonLauncher.Source @())) {
      return @{
        Source = $pythonLauncher.Source
        Arguments = @()
      }
  }
  throw 'Python was not found.'
}

$python = Find-Python
Push-Location $repoRoot
try {
  & $python.Source @($python.Arguments) -m compileall -q codex_sync tests
  if ($LASTEXITCODE -ne 0) { throw 'Python compilation check failed.' }
  & $python.Source @($python.Arguments) -m unittest discover -s tests -q
  if ($LASTEXITCODE -ne 0) { throw 'Unit tests failed.' }
  & $python.Source @($python.Arguments) -c "from codex_sync.cli import build_parser; assert build_parser().parse_args(['queue-status']).command == 'queue-status'"
  if ($LASTEXITCODE -ne 0) { throw 'CLI parser smoke test failed.' }
  Write-Output 'Windows smoke test OK'
} finally {
  Pop-Location
}
