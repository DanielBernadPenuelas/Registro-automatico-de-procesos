@echo off

setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 > nul
title Order.bat - proceso batch

pushd "%~dp0"

set "TASK_NAME=%~1"
if "%TASK_NAME%"=="" set "TASK_NAME=TAREA_DEMO"

set "PARAM_1=%~2"

set "AZSQL_SERVER=miservidor.database.windows.net"
set "AZSQL_DATABASE=mibasededatos"
set "AZSQL_DRIVER=ODBC Driver 18 for SQL Server"
set "AZSQL_TIMEOUT=30"

set "AZSQL_AUTH=aad"

if exist "config_secreto.bat" call "config_secreto.bat"

set "PY_ENV=%~dp0.venv\Scripts\python.exe"
set "LOG_DIR=%~dp0logs"
set "AZSQL_PENDING_DIR=%LOG_DIR%"
set "LOCK_FILE=%LOG_DIR%\%TASK_NAME%.lock"

set "PROCESO=procesos\mi_proceso.py"

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

for /f %%T in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "TS=%%T"
set "LOG_FILE=%LOG_DIR%\%TASK_NAME%_%TS%.log"


if exist "%PY_ENV%" (
    set "PYTHON=%PY_ENV%"
) else (
    where python > nul 2>&1
    if errorlevel 1 (
        echo [ERROR] No se encontro Python en el PATH ni en %PY_ENV%.
        set "EXIT_CODE=11"
        goto :fin
    )
    set "PYTHON=python"
)

if "%AZSQL_SERVER%"=="" (
    echo [ERROR] AZSQL_SERVER sin definir.
    set "EXIT_CODE=12"
    goto :fin
)
if "%AZSQL_DATABASE%"=="" (
    echo [ERROR] AZSQL_DATABASE sin definir.
    set "EXIT_CODE=12"
    goto :fin
)

if exist "%LOCK_FILE%" (
    echo [AVISO] Ya hay una ejecucion en curso de %TASK_NAME%.
    type "%LOCK_FILE%"
    echo         Si es un bloqueo huerfano, borra: %LOCK_FILE%
    set "EXIT_CODE=10"
    goto :fin
)
echo Iniciada el %DATE% %TIME% en %COMPUTERNAME% (PID cmd %RANDOM%) > "%LOCK_FILE%"

call "%PYTHON%" logger_file.py --task "%TASK_NAME%" -- "%PYTHON%" "%PROCESO%" %PARAM_1% > "%LOG_FILE%" 2>&1
set "EXIT_CODE=%ERRORLEVEL%"

type "%LOG_FILE%"

if exist "%LOCK_FILE%" del /q "%LOCK_FILE%"

  
if exist "%AZSQL_PENDING_DIR%\task_log_pendiente.jsonl" (
    echo [INFO] Reenviando registros pendientes...
    call "%PYTHON%" logger_file.py --flush-pending >> "%LOG_FILE%" 2>&1
)

:fin
if "%EXIT_CODE%"=="0" (
    echo [OK]    %TASK_NAME% finalizo correctamente el %DATE% %TIME%.
) else (
    echo [ERROR] %TASK_NAME% finalizo con codigo %EXIT_CODE% el %DATE% %TIME%.
    echo         Revisa ops.TASK_LOG y el log local: %LOG_FILE%
)

popd
endlocal & exit /b %EXIT_CODE%
