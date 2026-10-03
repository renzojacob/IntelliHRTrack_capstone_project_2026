$ProjectDirectory = $PSScriptRoot
$PythonExecutable = Join-Path $ProjectDirectory ".venv\Scripts\python.exe"
$ConnectorScript = Join-Path $ProjectDirectory "hikvision_connector.py"
$LogFile = Join-Path $ProjectDirectory "hikvision_connector.log"

Set-Location $ProjectDirectory

& $PythonExecutable -u $ConnectorScript --poll-seconds 10 *>> $LogFile