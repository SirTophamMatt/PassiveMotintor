# Builds the standalone desktop app into dist\PassiveMonitor\PassiveMonitor.exe
# Run from the unified_monitor folder:  .\build_exe.ps1
Write-Host "Building Passive Monitor desktop executable..." -ForegroundColor Cyan
python -m PyInstaller PassiveMonitor.spec --noconfirm --clean
if ($LASTEXITCODE -eq 0) {
    Write-Host "`nBuild complete: dist\PassiveMonitor\PassiveMonitor.exe" -ForegroundColor Green
    Write-Host "Double-click that file to launch (no command line needed)."
} else {
    Write-Host "`nBuild failed (exit $LASTEXITCODE). See output above." -ForegroundColor Red
}
