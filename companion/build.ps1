# Builds the installer on a Windows PC, as the GitHub workflow does: Python 3.12+ and Inno Setup 6
# installed. Run from this folder:  powershell -ExecutionPolicy Bypass -File .\build.ps1
# Or, to try it without building:  py -3 -m pip install -r requirements.txt; pyw -3 dodobe_companion.pyw
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
py -3 ..\tests\test_config.py
py -3 -m pip install --quiet pyinstaller==6.11.1 -r requirements.txt
py -3 -m PyInstaller --noconfirm --clean dodobe-companion.spec
& "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe" /O"Output" dodobe-companion.iss
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }
Get-ChildItem Output
