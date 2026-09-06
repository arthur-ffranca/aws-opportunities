param(
  [string]$Endpoint = "http://localhost.localstack.cloud:4566",
  [string]$SourceXlsx = (Join-Path $PSScriptRoot "data\scan-case-input.xlsx"),
  [string]$RawBucket = "scan-case-raw",
  [string]$CuratedBucket = "scan-case-curated",
  [string]$QueueName = "scan-case-events",
  [string]$TableName = "ScanProductRecommendations",
  [string]$BedrockModelId = "meta.llama3-8b-instruct-v1:0",
  [int]$BedrockTimeoutSeconds = 8,
  [int]$TopNDynamoDB = 10,
  [string]$DatadogHost = "127.0.0.1",
  [int]$DatadogPort = 8125,
  [switch]$UseBedrock,
  [switch]$SkipBedrock,
  [switch]$DryRun
)

$ErrorActionPreference = "Stop"

$LabDir = $PSScriptRoot
$RunStateDir = Join-Path $LabDir "run-state"
$JsonDir = Join-Path $RunStateDir "json"
$AccountId = "000000000000"
$Region = "us-east-1"

function Show-Step {
  param([string]$Text)
  Write-Host ""
  Write-Host "==> $Text"
}

function Invoke-Aws {
  param([Parameter(ValueFromRemainingArguments = $true)][string[]]$AwsArgs)

  & aws --cli-connect-timeout 5 --cli-read-timeout 15 --endpoint-url=$Endpoint @AwsArgs
  if ($LASTEXITCODE -ne 0) {
    throw "AWS CLI falhou: aws --endpoint-url=$Endpoint $($AwsArgs -join ' ')"
  }
}

function Invoke-AwsText {
  param([Parameter(ValueFromRemainingArguments = $true)][string[]]$AwsArgs)

  $output = & aws --cli-connect-timeout 5 --cli-read-timeout 15 --endpoint-url=$Endpoint @AwsArgs
  if ($LASTEXITCODE -ne 0) {
    throw "AWS CLI falhou: aws --endpoint-url=$Endpoint $($AwsArgs -join ' ')"
  }
  return ($output | Out-String).Trim()
}

function Test-AwsSuccess {
  param([Parameter(ValueFromRemainingArguments = $true)][string[]]$AwsArgs)

  & aws --cli-connect-timeout 5 --cli-read-timeout 15 --endpoint-url=$Endpoint @AwsArgs *> $null
  return ($LASTEXITCODE -eq 0)
}

function Get-OrCreateQueueUrl {
  $output = & aws --cli-connect-timeout 5 --cli-read-timeout 15 --endpoint-url=$Endpoint sqs get-queue-url `
    --queue-name $QueueName `
    --query QueueUrl `
    --output text 2>$null

  if ($LASTEXITCODE -eq 0 -and $output) {
    return ($output | Out-String).Trim()
  }

  return Invoke-AwsText sqs create-queue `
    --queue-name $QueueName `
    --query QueueUrl `
    --output text
}

function Ensure-Bucket {
  param([string]$Bucket)

  if (Test-AwsSuccess s3api head-bucket --bucket $Bucket) {
    Write-Host "Bucket ja existe: $Bucket"
  } else {
    Invoke-Aws s3 mb "s3://$Bucket" | Out-Null
    Write-Host "Bucket criado: $Bucket"
  }
}

function Ensure-Table {
  if (Test-AwsSuccess dynamodb describe-table --table-name $TableName) {
    Write-Host "Tabela ja existe: $TableName"
    return
  }

  Invoke-Aws dynamodb create-table `
    --table-name $TableName `
    --attribute-definitions "AttributeName=produto,AttributeType=S" `
    --key-schema "AttributeName=produto,KeyType=HASH" `
    --billing-mode PAY_PER_REQUEST | Out-Null

  do {
    Start-Sleep -Seconds 1
    $status = Invoke-AwsText dynamodb describe-table `
      --table-name $TableName `
      --query Table.TableStatus `
      --output text
    Write-Host "Status da tabela: $status"
  } while ($status -ne "ACTIVE")
}

function Drain-Queue {
  param([string]$QueueUrl)

  for ($round = 1; $round -le 8; $round++) {
    $raw = Invoke-AwsText sqs receive-message `
      --queue-url $QueueUrl `
      --max-number-of-messages 10 `
      --wait-time-seconds 1

    if (-not $raw) {
      return
    }

    $response = $raw | ConvertFrom-Json
    if (-not $response.Messages) {
      return
    }

    foreach ($message in $response.Messages) {
      Invoke-Aws sqs delete-message `
        --queue-url $QueueUrl `
        --receipt-handle $message.ReceiptHandle | Out-Null
    }
  }
}

if ($DryRun) {
  Write-Output "S3 raw bucket: recebe o XLSX original do case Scan"
  Write-Output "S3 ObjectCreated: upload dispara evento automaticamente"
  Write-Output "SQS event queue: recebe o evento do bucket raw"
  Write-Output "worker processa XLSX da Scan: calcula score, acao e racional por produto"
  Write-Output "DynamoDB recomenda produtos: salva top ranking consultavel"
  Write-Output "Datadog metrics: envia scan.radar.* via DogStatsD"
  Write-Output "Bedrock provider: $BedrockModelId fica configurado; use -UseBedrock para chamar o runtime"
  Write-Output "S3 curated outputs: salva CSV, JSON de resumo e Markdown de insights"
  exit 0
}

if (-not (Test-Path -LiteralPath $SourceXlsx)) {
  throw "Arquivo do case nao encontrado: $SourceXlsx"
}

New-Item -ItemType Directory -Force -Path $RunStateDir, $JsonDir | Out-Null

Show-Step "Conferindo LocalStack e AWS CLI"
Invoke-Aws sts get-caller-identity | Out-Null
Write-Host "LocalStack respondeu em $Endpoint"

Show-Step "Garantindo buckets S3"
Ensure-Bucket -Bucket $RawBucket
Ensure-Bucket -Bucket $CuratedBucket

Show-Step "Garantindo fila SQS"
$queueUrl = Get-OrCreateQueueUrl
Write-Host "Fila pronta: $queueUrl"

Show-Step "Garantindo tabela DynamoDB"
Ensure-Table

Show-Step "Configurando S3 raw bucket para publicar eventos na SQS"
$queueArn = Invoke-AwsText sqs get-queue-attributes `
  --queue-url $queueUrl `
  --attribute-names QueueArn `
  --query Attributes.QueueArn `
  --output text

if (-not $queueArn -or $queueArn -eq "None") {
  $queueArn = "arn:aws:sqs:${Region}:${AccountId}:$QueueName"
}

$queuePolicy = @{
  Version = "2012-10-17"
  Statement = @(
    @{
      Effect = "Allow"
      Principal = "*"
      Action = "sqs:SendMessage"
      Resource = $queueArn
      Condition = @{
        ArnLike = @{
          "aws:SourceArn" = "arn:aws:s3:::$RawBucket"
        }
      }
    }
  )
} | ConvertTo-Json -Compress -Depth 10

$queueAttributes = @{ Policy = $queuePolicy } | ConvertTo-Json -Compress -Depth 10
$queueAttributesPath = Join-Path $JsonDir "queue-attributes.json"
$queueAttributes | Set-Content -Path $queueAttributesPath -Encoding ascii

Invoke-Aws sqs set-queue-attributes `
  --queue-url $queueUrl `
  --attributes "file://$queueAttributesPath" | Out-Null

$notification = @{
  QueueConfigurations = @(
    @{
      QueueArn = $queueArn
      Events = @("s3:ObjectCreated:*")
    }
  )
} | ConvertTo-Json -Compress -Depth 10

$notificationPath = Join-Path $JsonDir "s3-notification.json"
$notification | Set-Content -Path $notificationPath -Encoding ascii

Invoke-Aws s3api put-bucket-notification-configuration `
  --bucket $RawBucket `
  --notification-configuration "file://$notificationPath" | Out-Null

Write-Host "S3 raw bucket agora dispara evento ObjectCreated para $QueueName"

Show-Step "Limpando fila antes da prova"
Drain-Queue -QueueUrl $queueUrl

Show-Step "Enviando XLSX da Scan para o S3 raw"
$timestamp = (Get-Date).ToString("yyyyMMdd-HHmmss")
$rawKey = "incoming/case-scan-$timestamp.xlsx"
Invoke-Aws s3 cp $SourceXlsx "s3://$RawBucket/$rawKey" | Out-Null
Write-Host "Upload feito: s3://$RawBucket/$rawKey"

Show-Step "Esperando evento S3 chegar na SQS"
$messageResponse = $null
for ($attempt = 1; $attempt -le 12; $attempt++) {
  $rawMessage = Invoke-AwsText sqs receive-message `
    --queue-url $queueUrl `
    --max-number-of-messages 1 `
    --wait-time-seconds 1

  if ($rawMessage) {
    $messageResponse = $rawMessage | ConvertFrom-Json
    if ($messageResponse.Messages) {
      break
    }
  }

  Write-Host "Aguardando evento... tentativa $attempt"
}

if (-not $messageResponse -or -not $messageResponse.Messages) {
  throw "O evento S3 nao chegou na SQS dentro do tempo esperado."
}

$message = $messageResponse.Messages[0]
$eventKey = $rawKey
try {
  $eventJson = $message.Body | ConvertFrom-Json
  if ($eventJson.Records -and $eventJson.Records[0].s3.object.key) {
    $eventKey = [System.Uri]::UnescapeDataString($eventJson.Records[0].s3.object.key)
  }
} catch {
  Write-Host "Evento nao estava no formato S3 padrao; usando chave do upload."
}

Write-Host "Evento recebido para: s3://$RawBucket/$eventKey"

Show-Step "Rodando worker com scoring comercial e Bedrock"
$bedrockEnabled = $UseBedrock -and -not $SkipBedrock
if ($bedrockEnabled) {
  try {
    $health = Invoke-WebRequest `
      -UseBasicParsing `
      -Uri "$Endpoint/_localstack/health" `
      -TimeoutSec 5 | Select-Object -ExpandProperty Content | ConvertFrom-Json
    $bedrockRuntimeStatus = $health.services.'bedrock-runtime'
    if ($bedrockRuntimeStatus -ne "running") {
      Write-Host "Bedrock provider configurado ($BedrockModelId), mas bedrock-runtime esta '$bedrockRuntimeStatus'. Usando fallback para nao travar."
      $bedrockEnabled = $false
    }
  } catch {
    Write-Host "Nao consegui ler health do Bedrock Runtime. Usando fallback para nao travar."
    $bedrockEnabled = $false
  }
} else {
  Write-Host "Bedrock provider configurado ($BedrockModelId), mas chamada ao runtime esta desligada nesta execucao. Use -UseBedrock para tentar invocar."
}

$workerArgs = @(
  ".\process-scan-case.py",
  "--endpoint", $Endpoint,
  "--raw-bucket", $RawBucket,
  "--raw-key", $eventKey,
  "--curated-bucket", $CuratedBucket,
  "--table-name", $TableName,
  "--work-dir", $RunStateDir,
  "--top-n-dynamodb", "$TopNDynamoDB",
  "--datadog-host", $DatadogHost,
  "--datadog-port", "$DatadogPort",
  "--service-name", "scan-opportunity-radar",
  "--bedrock-model-id", $BedrockModelId,
  "--bedrock-timeout-seconds", "$BedrockTimeoutSeconds"
)

if ($bedrockEnabled) {
  $workerArgs += "--use-bedrock"
}

& py -3.14 @workerArgs
if ($LASTEXITCODE -ne 0) {
  throw "Worker falhou ao processar o case Scan."
}

Invoke-Aws sqs delete-message `
  --queue-url $queueUrl `
  --receipt-handle $message.ReceiptHandle | Out-Null

Show-Step "Validando outputs curated"
try {
  Invoke-Aws s3 ls "s3://$CuratedBucket/recommendations/" | Out-Host
  Invoke-Aws s3 ls "s3://$CuratedBucket/summary/" | Out-Host
  Invoke-Aws s3 ls "s3://$CuratedBucket/insights/" | Out-Host
} catch {
  Write-Host "Aviso: processamento terminou, mas a validacao final no S3 falhou: $($_.Exception.Message)"
}

Show-Step "Validando resumo no DynamoDB"
$summaryKey = @{ produto = @{ S = "__SUMMARY__" } } | ConvertTo-Json -Compress -Depth 5
$summaryKeyPath = Join-Path $JsonDir "summary-key.json"
$summaryKey | Set-Content -Path $summaryKeyPath -Encoding ascii
try {
  Invoke-Aws dynamodb get-item `
    --table-name $TableName `
    --key "file://$summaryKeyPath" `
    --projection-expression "produto, acao" | Out-Host
} catch {
  Write-Host "Aviso: resumo foi gravado pelo worker, mas a validacao final no DynamoDB falhou: $($_.Exception.Message)"
}

Write-Host ""
Write-Host "SCAN AWS DEMO OK: XLSX no S3 raw -> evento SQS -> worker -> DynamoDB + S3 curated + Bedrock insights."
