param(
  [switch]$Clean,
  [switch]$SkipTests,
  [string]$OutputDir = 'dist'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$outputPath = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $OutputDir))
$buildPath = [System.IO.Path]::GetFullPath((Join-Path $repoRoot 'build\pyinstaller'))

function Assert-UnderRepo([string]$Path) {
  $rootWithSeparator = $repoRoot.TrimEnd('\') + '\'
  if (-not $Path.StartsWith($rootWithSeparator, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to operate outside the repository: $Path"
  }
}

Assert-UnderRepo $outputPath
Assert-UnderRepo $buildPath

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
  throw 'Python 3.10 or newer was not found.'
}

$python = Find-Python
& $python.Source @($python.Arguments) -c "import sys; assert sys.version_info >= (3, 10), sys.version"
if ($LASTEXITCODE -ne 0) {
  throw 'Python 3.10 or newer is required.'
}

if ($Clean) {
  foreach ($path in @($outputPath, $buildPath)) {
    if (Test-Path -LiteralPath $path) {
      Remove-Item -LiteralPath $path -Recurse -Force
    }
  }
}

if (-not $SkipTests) {
  & $python.Source @($python.Arguments) -m unittest discover -s tests -q
  if ($LASTEXITCODE -ne 0) {
    throw 'Automated tests failed.'
  }
}

& $python.Source @($python.Arguments) -c "import PySide6, PyInstaller"
if ($LASTEXITCODE -ne 0) {
  throw 'Build dependencies are missing. Install with: python -m pip install .[gui,build]'
}

New-Item -ItemType Directory -Force -Path $outputPath | Out-Null
New-Item -ItemType Directory -Force -Path $buildPath | Out-Null
& $python.Source @($python.Arguments) -m PyInstaller `
  --noconfirm `
  --clean `
  --onefile `
  --windowed `
  --name CodexHistorySync `
  --distpath $outputPath `
  --workpath $buildPath `
  --specpath $buildPath `
  (Join-Path $repoRoot 'launch_gui.py')
if ($LASTEXITCODE -ne 0) {
  throw 'PyInstaller failed.'
}

$exe = Join-Path $outputPath 'CodexHistorySync.exe'
if (-not (Test-Path -LiteralPath $exe)) {
  throw "Expected executable was not created: $exe"
}
Write-Output "Created: $exe"
