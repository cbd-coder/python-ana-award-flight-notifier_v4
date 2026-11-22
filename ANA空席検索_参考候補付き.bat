@echo off
chcp 65001 >nul
title ANA Award Availability - With Reference Dates
cd /d "C:\GitHub\kadai_self"
py -3 ana_award_submission_with_alternatives.py
if errorlevel 1 echo Python exited with an error. See the output above.
echo.
pause
