@echo off
cd /d "%~dp0"
set PY=python
where py >nul 2>nul && set PY=py
%PY% -c "import flask" 2>nul || %PY% -m pip install Flask
rem Open the browser after the server has had a moment to start
start "" /b cmd /c "timeout /t 2 /nobreak >nul & start http://127.0.0.1:8080/"
%PY% app.py
pause
