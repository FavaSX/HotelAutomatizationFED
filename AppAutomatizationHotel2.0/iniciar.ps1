# Levanta en local la app de conciliacion del Hotel Plaza San Francisco (Windows / PowerShell).
# Uso: doble clic en iniciar.bat, o desde PowerShell: .\iniciar.ps1
# (Texto sin tildes a proposito: PowerShell 5.1 lee mal los .ps1 UTF-8 sin BOM)

$ErrorActionPreference = "Stop"

function Fail($message) {
    Write-Host $message -ForegroundColor Red
    Read-Host "Presiona Enter para cerrar"
    exit 1
}

Write-Host "==> [1/6] Entrando al proyecto" -ForegroundColor Cyan
Set-Location $PSScriptRoot

Write-Host "==> [2/6] Preparando el entorno virtual (.venv)" -ForegroundColor Cyan
$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Host "    No existe .venv: lo creo ahora"
    if (Get-Command py -ErrorAction SilentlyContinue) { py -3 -m venv .venv } else { python -m venv .venv }
    if (-not (Test-Path $python)) { Fail "No pude crear .venv. Instala Python 3 desde python.org (marca 'Add to PATH')." }
}

# Modo desarrollo: sin esto la app no arranca en local (exige DJANGO_SECRET_KEY)
$env:DJANGO_DEBUG = "True"

Write-Host "==> [3/6] Actualizando el codigo desde GitHub" -ForegroundColor Cyan
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Host "    Git no esta instalado: me salto el git pull."
} elseif (git status --porcelain) {
    Write-Host "    Hay cambios sin commitear: me salto el git pull para no tocar tu trabajo."
} else {
    git pull --ff-only
    if ($LASTEXITCODE -ne 0) { Write-Host "    No se pudo hacer git pull (sin internet o ramas divergentes?). Sigo con el codigo local." }
}

Write-Host "==> [4/6] Instalando/actualizando dependencias" -ForegroundColor Cyan
& $python -m pip install -q -r requirements.txt
if ($LASTEXITCODE -ne 0) { Fail "Fallo pip install." }

Write-Host "==> [5/6] Aplicando migraciones" -ForegroundColor Cyan
& $python manage.py migrate
if ($LASTEXITCODE -ne 0) { Fail "Fallo migrate." }

Write-Host "==> [6/6] Levantando el servidor en http://127.0.0.1:8000 (Ctrl+C para detener)" -ForegroundColor Cyan
& $python manage.py runserver
