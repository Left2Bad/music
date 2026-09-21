@echo off
setlocal EnableExtensions

pushd "%~dp0"

set "PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%PYTHON%" set "PYTHON=python"

"%PYTHON%" "%~dp0(0)instruments.py"
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
	echo.
	echo Программа завершилась с ошибкой %EXIT_CODE%.
	pause
)

popd
exit /b %EXIT_CODE%