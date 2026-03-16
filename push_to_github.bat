@echo off
setlocal

cd /d "%~dp0"

git rev-parse --is-inside-work-tree >nul 2>&1
if errorlevel 1 (
    echo This file must stay inside your Git repository.
    pause
    exit /b 1
)

set "COMMIT_MESSAGE="
set /p COMMIT_MESSAGE=Commit message ^(leave blank for auto message^): 
if not defined COMMIT_MESSAGE (
    set "COMMIT_MESSAGE=Update %DATE% %TIME%"
)

echo.
echo Staging all tracked and untracked changes...
git add -A
if errorlevel 1 (
    echo git add failed.
    pause
    exit /b 1
)

git diff --cached --quiet
if errorlevel 1 goto commit_changes

echo No changes to commit.
pause
exit /b 0

:commit_changes
echo.
echo Creating commit...
git commit -m "%COMMIT_MESSAGE%"
if errorlevel 1 (
    echo Commit failed. Resolve the issue shown above and run this file again.
    pause
    exit /b 1
)

echo.
echo Pushing to GitHub...
git push origin HEAD
if errorlevel 1 (
    echo Push failed. Check your GitHub authentication or remote branch state.
    pause
    exit /b 1
)

echo.
echo Push complete.
pause
