@echo off
rem Garante PATH do sistema mesmo quando chamado de shells POSIX (git-bash)
set "PATH=%SystemRoot%\System32;%SystemRoot%;%SystemRoot%\System32\Wbem;%PATH%"
chcp 65001 >nul
setlocal EnableExtensions
title Flash Mem - Instalador

echo ============================================================
echo   FLASH MEM - Instalador
echo   Gerenciador de memoria RAM informado, seletivo e verificado
echo ============================================================
echo.

set "SRC=%~dp0"
set "DEST=%LOCALAPPDATA%\FlashMem"
set "SMENU=%APPDATA%\Microsoft\Windows\Start Menu\Programs"
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "DESKTOP=%USERPROFILE%\Desktop"

rem ---------- 1. detecta python (nao obrigatorio se exe ja existe) ----------
set "PYCMD="
py -3 --version >nul 2>&1 && set "PYCMD=py -3"
if not defined PYCMD python --version >nul 2>&1 && set "PYCMD=python"

if not defined PYCMD if not exist "%SRC%dist\flashmem.exe" (
    echo ERRO: Python nao encontrado e nem dist\flashmem.exe presente.
    echo        Instale Python em https://python.org ou gere o .exe antes.
    pause
    exit /b 1
)
if not defined PYCMD echo AVISO: Python ausente - usando apenas o .exe existente.

rem ---------- 2. builda o .exe se nao existir ----------
set "TARGET=%SRC%dist\flashmem.exe"
if not exist "%TARGET%" (
    if defined PYCMD (
        echo [2/5] Gerando flashmem.exe com PyInstaller ^(pode demorar~1 min^)...
        %PYCMD% -m PyInstaller --onefile --noconsole --name flashmem ^
            --distpath "%SRC%dist" --workpath "%SRC%build_pyinstaller" ^
            --specpath "%SRC%" "%SRC%run_cli.py" >nul 2>&1
        if not exist "%TARGET%" (
            %PYCMD% -m PyInstaller --version >nul 2>&1
            if errorlevel 1 (
                echo PyInstaller ausente. Instalar agora via pip --user?
                choice /c YN /m "Instalar PyInstaller"
                if errorlevel 2 (
                    echo Sem build - instalando em MODO PYTHON ^(atalho usa python^).
                ) else (
                    %PYCMD% -m pip install --user pyinstaller >nul 2>&1
                    %PYCMD% -m PyInstaller --onefile --noconsole --name flashmem ^
                        --distpath "%SRC%dist" --workpath "%SRC%build_pyinstaller" ^
                        --specpath "%SRC%" "%SRC%run_cli.py" >nul 2>&1
                )
            )
        )
    )
)

rem ---------- 3. copia arquivos ----------
echo [3/5] Copiando arquivos para %DEST% ...
if exist "%DEST%" rmdir /s /q "%DEST%" >nul 2>&1
mkdir "%DEST%" 2>nul
xcopy "%SRC%flashmem" "%DEST%\flashmem\" /e /i /q >nul
copy /y "%SRC%run_cli.py" "%DEST%\" >nul
copy /y "%SRC%README.md" "%DEST%\" >nul
if exist "%TARGET%" (
    mkdir "%DEST%\dist" 2>nul
    copy /y "%TARGET%" "%DEST%\dist\" >nul
)
if exist "%SRC%assets\flashmem.ico" (
    mkdir "%DEST%\assets" 2>nul
    copy /y "%SRC%assets\flashmem.ico" "%DEST%\assets\" >nul
    set "ICON=%DEST%\assets\flashmem.ico"
) else (
    set "ICON="
)

rem ---------- 4. atalhos ----------
echo [4/5] Criando atalhos...
set "PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%PS%" set "PS=powershell"
set "MODE=exe"
if exist "%DEST%\dist\flashmem.exe" (
    set "TARGETPATH=%DEST%\dist\flashmem.exe"
    set "ARGS=tray"
) else (
    set "MODE=python"
    set "TARGETPATH=%LOCALAPPDATA%\Microsoft\Windows\py.exe"
    if not exist "%TARGETPATH%" for %%i in (pythonw.exe) do set "TARGETPATH=%%~$PATH:i"
    rem pythonw = SEM janela de console (app de bandeja roda invisivel em background)
    set "ARGS=-m flashmem.cli tray"
)
if not defined ICON set "ICON=%TARGETPATH%,0"

"%PS%" -NoProfile -Command "$w = New-Object -ComObject WScript.Shell; foreach ($p in @('%DESKTOP%\FlashMem.lnk','%SMENU%\FlashMem.lnk')) { $s = $w.CreateShortcut($p); $s.TargetPath = '%TARGETPATH%'; $s.Arguments = '%ARGS%'; $s.WorkingDirectory = '%DEST%'; $s.IconLocation = '%ICON%'; $s.Description = 'Flash Mem - gerenciador de memoria RAM'; $s.Save() }" >nul 2>&1

rem ---------- 5. autoinicio ----------
echo [5/5] Autoiniciar com o Windows?
choice /c YN /m "Criar atalho na pasta de inicializacao"
if not errorlevel 2 (
    "%PS%" -NoProfile -Command "$w = New-Object -ComObject WScript.Shell; $s = $w.CreateShortcut('%STARTUP%\FlashMem.lnk'); $s.TargetPath = '%TARGETPATH%'; $s.Arguments = '%ARGS%'; $s.WorkingDirectory = '%DEST%'; $s.IconLocation = '%ICON%'; $s.Save()" >nul 2>&1
    echo Atalho de autoinicio criado.
) else (
    echo Autoinicio NAO criado ^(pode criar depois rodando install de novo^).
)

echo.
echo ============================================================
echo   INSTALADO COM SUCESSO (modo: %MODE%)
echo.
echo   Pasta        : %DEST%
echo   Atalhos     : Desktop e Menu Iniciar ("FlashMem")
echo   Usar        : clique duplo no atalho (bandeja) ou
echo                  %DEST%\dist\flashmem.exe status
echo   Desinstalar : rode uninstall.bat na pasta do projeto
echo ============================================================
pause
exit /b 0
