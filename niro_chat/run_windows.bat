@echo off
setlocal
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Criando ambiente virtual...
  py -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
if "%NIRO_SECRET%"=="" (
  set "SECRETFILE=%USERPROFILE%\.niro_chat_secret"
  if not exist "%USERPROFILE%\.niro_chat_secret" (
    for /f %%i in ('python -c "import secrets; print(secrets.token_urlsafe(48))"') do echo %%i>"%USERPROFILE%\.niro_chat_secret"
  )
  set /p NIRO_SECRET=<"%USERPROFILE%\.niro_chat_secret"
)
if "%NIRO_DATA_DIR%"=="" set NIRO_DATA_DIR=%USERPROFILE%\.niro_chat
uvicorn main:app --host 127.0.0.1 --port 8000
