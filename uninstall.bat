@echo off
rem Garante PATH do sistema mesmo quando chamado de shells POSIX (git-bash)
set "PATH=%SystemRoot%\System32;%SystemRoot%;%SystemRoot%\System32\Wbem;%PATH%"
chcp 65001 >nul
setlocal EnableExtensions
title Flash Mem - Desinstalador

echo ============================================================
echo   FLASH MEM - Desinstalador
echo ============================================================
echo.

set "DEST=%LOCALAPPDATA%\FlashMem"
set "SMENU=%APPDATA%\Microsoft\Windows\Start Menu\Programs"
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "DESKTOP=%USERPROFILE%\Desktop"
set "BACKUP=%USERPROFILE%\Documents\FlashMem-backup"

rem ---------- 1. encerra o app ----------
echo [1/4] Encerrando Flash Mem, se estiver rodando...
taskkill /IM flashmem.exe /F >nul 2>&1
timeout /t 1 /nobreak >nul

rem ---------- 2. backup opcional de historico/config ----------
if exist "%DEST%\flashmem_history.jsonl" (
    echo [2/4] Foi encontrado historico de limpezas / configuracao do plano.
    choice /c YN /m "Fazer backup em Documents\FlashMem-backup antes de apagar"
    if not errorlevel 2 (
        mkdir "%BACKUP%" 2>nul
        copy /y "%DEST%\flashmem_history.jsonl" "%BACKUP%\" >nul 2>&1
        copy /y "%DEST%\flashmem_schedule.json" "%BACKUP%\" >nul 2>&1
        echo Backup salvo em %BACKUP%
    ) else (
        echo Sem backup - historico sera apagado.
    )
) else (
    echo [2/4] Sem historico para preservar.
)

rem ---------- 3. remove arquivos e atalhos ----------
echo [3/4] Removendo arquivos e atalhos...
if exist "%DEST%" rmdir /s /q "%DEST%"
del "%DESKTOP%\FlashMem.lnk" 2>nul
del "%SMENU%\FlashMem.lnk" 2>nul
del "%STARTUP%\FlashMem.lnk" 2>nul

rem ---------- 4. confirma ----------
echo [4/4] Verificando...
if exist "%DEST%" (
    echo AVISO: nao consegui remover %DEST% - feche o app e rode de novo.
    pause
    exit /b 1
)
echo.
echo ============================================================
echo   FLASH MEM DESINSTALADO
echo.
echo   Removido : %DEST%
echo   Removido : atalhos (Desktop, Menu Iniciar, Autoinicio)
if exist "%BACKUP%" echo   Backup   : %BACKUP% ^(mantido^)
echo   O codigo-fonte clonado NAO e tocado por este desinstalador.
echo ============================================================
pause
exit /b 0
