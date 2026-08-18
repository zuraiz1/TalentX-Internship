# run_news_briefing.ps1
# Wrapper called by Windows Task Scheduler every hour.
# Runs fetch + brief, logs everything (with timestamps) so failures are visible later
# instead of vanishing into a scheduled task that ran invisibly in the background.

# --- EDIT THIS to match where news_briefing.py actually lives ---
$ScriptDir = "C:\Users\zurai\Desktop\TalentX\Overwatch"
# ------------------------------------------------------------------

$LogDir = Join-Path $ScriptDir "logs"
$BriefingDir = Join-Path $ScriptDir "briefings"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
New-Item -ItemType Directory -Force -Path $BriefingDir | Out-Null

$Timestamp = Get-Date -Format "yyyy-MM-dd_HH-mm"
$LogFile = Join-Path $LogDir "run_$Timestamp.log"

Set-Location $ScriptDir

"===== Run started: $(Get-Date) =====" | Out-File -FilePath $LogFile -Encoding utf8

try {
    python news_briefing.py fetch *>> $LogFile
    python news_briefing.py brief --hours 6 --save $BriefingDir *>> $LogFile
    "===== Run finished OK: $(Get-Date) =====" | Out-File -FilePath $LogFile -Append -Encoding utf8
} catch {
    "===== Run FAILED: $(Get-Date) =====" | Out-File -FilePath $LogFile -Append -Encoding utf8
    $_ | Out-File -FilePath $LogFile -Append -Encoding utf8
}

# Keep only the last 7 days of logs so this doesn't grow forever
Get-ChildItem $LogDir -Filter "run_*.log" |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-7) } |
    Remove-Item -Force
