$ErrorActionPreference = "Stop"

$scriptPath = Join-Path $PSScriptRoot "..\run-scan-demo.ps1"

if (-not (Test-Path $scriptPath)) {
  throw "run-scan-demo.ps1 nao existe ainda."
}

$output = & $scriptPath -DryRun | Out-String

foreach ($expected in @(
  "S3 raw bucket",
  "S3 ObjectCreated",
  "SQS event queue",
  "worker processa XLSX da Scan",
  "DynamoDB recomenda produtos",
  "S3 curated outputs"
)) {
  if ($output -notmatch [regex]::Escape($expected)) {
    throw "Saida do DryRun nao contem: $expected"
  }
}

Write-Host "Smoke test OK: demo AWS da Scan expõe pipeline real."
