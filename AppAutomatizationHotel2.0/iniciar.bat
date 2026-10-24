@echo off
rem Doble clic para levantar la app en local (ejecuta iniciar.ps1 sin cambiar la politica de scripts de Windows)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0iniciar.ps1"
pause
