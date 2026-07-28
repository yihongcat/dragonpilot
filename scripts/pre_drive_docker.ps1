param(
  [Parameter(Position = 0)]
  [ValidateSet("quick", "build", "replay", "sim", "all", "help")]
  [string]$Mode = "quick"
)

$ErrorActionPreference = "Stop"

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$composeFile = Join-Path $repoRoot "compose.pre-drive.yaml"
$installedDocker = "D:\Apps\DockerDesktop\resources\bin\docker.exe"
$installedDockerBin = Split-Path -Parent $installedDocker
$dockerCommand = Get-Command docker -ErrorAction SilentlyContinue

if ($null -ne $dockerCommand) {
  $docker = $dockerCommand.Source
} elseif (Test-Path -LiteralPath $installedDocker) {
  $docker = $installedDocker
  $env:Path = "$installedDockerBin;$env:Path"
} else {
  throw "Docker CLI was not found. Start or install Docker Desktop first."
}

if ($Mode -eq "help") {
  & $docker compose -f $composeFile run --rm pre-drive bash scripts/pre_drive_check.sh --help
  exit $LASTEXITCODE
}

Push-Location $repoRoot
try {
  & $docker info --format "Docker server {{.ServerVersion}} ({{.OSType}}/{{.Architecture}})"
  if ($LASTEXITCODE -ne 0) {
    throw "Docker Desktop is installed but its Linux engine is not ready."
  }

  Write-Output ""
  Write-Output "== Gate 0/1: host branch, diff, and Git LFS =="
  & git status --short --branch
  if ($LASTEXITCODE -ne 0) {
    throw "Failed to read the local Git status."
  }
  & git log -1 --oneline --decorate
  & git diff --check HEAD
  if ($LASTEXITCODE -ne 0) {
    throw "Git diff validation failed."
  }
  & git lfs fsck
  if ($LASTEXITCODE -ne 0) {
    throw "Git LFS validation failed."
  }
  & git lfs checkout
  if ($LASTEXITCODE -ne 0) {
    throw "Git LFS working-tree checkout failed."
  }

  $modelPath = Join-Path $repoRoot "openpilot\selfdrive\modeld\models\driving_supercombo.onnx"
  if (-not (Test-Path -LiteralPath $modelPath)) {
    throw "The driving model is missing: $modelPath"
  }
  if ((Get-Item -LiteralPath $modelPath).Length -lt 1000000) {
    throw "The driving model is too small and is probably an LFS pointer."
  }

  $dirtyState = @(& git status --porcelain)
  if ($dirtyState.Count -ne 0) {
    throw "Docker tests require a clean local commit. Commit the intended snapshot locally before running this command."
  }

  & $docker compose -f $composeFile build pre-drive
  if ($LASTEXITCODE -ne 0) {
    throw "Failed to build the pre-drive test image."
  }

  $syncCommand = "set -euo pipefail; find /workspace -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +; GIT_LFS_SKIP_SMUDGE=1 git clone --depth 1 file:///source /workspace; git -C /workspace lfs ls-files -n | tar -C /source -T - -cf - | tar -C /workspace -xf -; git -C /workspace status --short --branch"
  & $docker compose -f $composeFile run --rm pre-drive bash -lc $syncCommand
  if ($LASTEXITCODE -ne 0) {
    throw "Failed to create the isolated Linux test workspace."
  }

  $testCommand = "uv sync --python 3.12 --frozen --all-extras && bash scripts/pre_drive_check.sh $Mode"
  & $docker compose -f $composeFile run --rm pre-drive bash -lc $testCommand
  exit $LASTEXITCODE
} finally {
  Pop-Location
}
