@echo off
setlocal
cd /d "%~dp0"

echo Instalando dependencias...
py -3 -m pip install --upgrade pip
if errorlevel 1 goto error

py -3 -m pip install -r requirements.txt
if errorlevel 1 goto error

py -3 -m pip install pyinstaller
if errorlevel 1 goto error

echo Empaquetando aplicacion...
py -3 -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name RecolectorRecursos ^
  app.py
if errorlevel 1 goto error

echo.
echo Aplicacion creada en:
echo %CD%\dist\RecolectorRecursos.exe
echo.
echo FFmpeg y FFprobe deben estar instalados y disponibles en PATH.
pause
exit /b 0

:error
echo.
echo Hubo un error. Revisa el mensaje anterior.
pause
exit /b 1
