# ==== שומר סוכן המסכם — סבג נדל"ן ====
# רץ כל 5 דק' דרך Task Scheduler, עצמאי מהשירות.
# בודק את פעימת-הלב (state.json -> last_alive, מתעדכן כל 60 שנ').
# אם השירות מת/תקוע > STALE_SECONDS -> התראת טלגרם + הפעלה-מחדש אוטומטית.
# שולח הודעת התאוששות כשהשירות חוזר. מתריע פעם אחת לכל אירוע (לא מציק).

$ErrorActionPreference = 'Stop'
$Base         = $PSScriptRoot
$StatePath    = Join-Path $Base 'state.json'
$WdStatePath  = Join-Path $Base 'watchdog-monitor.state'
$StartBat     = Join-Path $Base 'start-agent.bat'
$LogDir       = Join-Path $Base 'logs'
$StaleSeconds = 300       # 5 דק' ללא פעימת-לב = מת/תקוע
$AutoRestart  = $true

function Read-EnvValue([string]$key) {
  $envFile = Join-Path $Base '.env'
  if (-not (Test-Path $envFile)) { return $null }
  foreach ($line in Get-Content $envFile -Encoding UTF8) {
    $t = $line.Trim()
    if ($t -eq '' -or $t.StartsWith('#') -or ($t -notmatch '=')) { continue }
    $k, $v = $t.Split('=', 2)
    if ($k.Trim() -eq $key) { return $v.Trim().Trim('"').Trim("'") }
  }
  return $null
}

function Send-Telegram([string]$text) {
  try {
    $token  = Read-EnvValue 'BOT_TOKEN'
    $chatId = $null
    if (Test-Path $StatePath) {
      $st = Get-Content $StatePath -Raw -Encoding UTF8 | ConvertFrom-Json
      $chatId = $st.chat_id
    }
    if (-not $chatId) { $chatId = Read-EnvValue 'NAHI_TELEGRAM_ID' }
    if (-not $token -or -not $chatId) { return }
    $body = @{ chat_id = $chatId; text = $text; disable_web_page_preview = $true } |
            ConvertTo-Json -Compress
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($body)
    Invoke-RestMethod -Method Post -Uri "https://api.telegram.org/bot$token/sendMessage" `
      -ContentType 'application/json; charset=utf-8' -Body $bytes -TimeoutSec 20 | Out-Null
  } catch { }
}

function Get-Heartbeat-Age {
  if (-not (Test-Path $StatePath)) { return $null }
  try {
    $st = Get-Content $StatePath -Raw -Encoding UTF8 | ConvertFrom-Json
    if (-not $st.last_alive) { return $null }
    $epoch = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    return [int]($epoch - [double]$st.last_alive)
  } catch { return $null }
}

function Read-WdDown {
  if (-not (Test-Path $WdStatePath)) { return $false }
  return ((Get-Content $WdStatePath -Raw).Trim() -eq 'down')
}
function Write-WdDown([bool]$down) {
  Set-Content -Path $WdStatePath -Value ($(if ($down) { 'down' } else { 'up' })) -Encoding ASCII
}

# --- הערכת מצב ---
$age       = Get-Heartbeat-Age
$isDown    = ($null -eq $age) -or ($age -gt $StaleSeconds)
$wasDown   = Read-WdDown
$stamp     = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'

if ($isDown -and -not $wasDown) {
  # מעבר: חי -> מת. מתריעים פעם אחת.
  $ageTxt = if ($null -eq $age) { 'אין חותמת פעימה (state.json חסר/תקול)' } else { "אין פעימת-לב $age שניות" }
  $msg = "🔴 סוכן המסכם כבוי!`n$ageTxt (סף $StaleSeconds שנ').`nזוהה: $stamp"
  if ($AutoRestart -and (Test-Path $StartBat)) {
    Start-Process -FilePath $StartBat -WorkingDirectory $Base -WindowStyle Hidden
    $msg += "`n🔄 מנסה להפעיל מחדש אוטומטית — אעדכן כשיעלה."
  }
  Send-Telegram $msg
  Write-WdDown $true
}
elseif (-not $isDown -and $wasDown) {
  # מעבר: מת -> חי. הודעת התאוששות.
  Send-Telegram "🟢 סוכן המסכם חזר לפעול. פעימת-לב תקינה ($age שנ'). $stamp"
  Write-WdDown $false
}
# אם המצב לא השתנה — לא שולחים כלום (לא מציק).

# רישום שקט של ריצת השומר
try {
  if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }
  $state = if ($isDown) { 'DOWN' } else { "OK(age=$age s)" }
  Add-Content -Path (Join-Path $LogDir 'watchdog.log') -Value "$stamp  $state" -Encoding UTF8
} catch { }
