@echo off
setlocal

cd /d "%~dp0"

echo Cleaning previous builds...

if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo Building QBO Extension Apps...

pipenv run pyinstaller ^
    --noconfirm ^
    --clean ^
    --onedir ^
    --windowed ^
    --name "QBO Extension Apps" ^
    --collect-all customtkinter ^
    --collect-all xlsxwriter ^
    --collect-all pdfplumber ^
    --collect-all fitz ^
    --collect-all PIL ^
    --hidden-import pytesseract ^
    --hidden-import fitz ^
    --hidden-import PIL.Image ^
    app.py

if errorlevel 1 (
    echo.
    echo Build failed.
    pause
    exit /b 1
)

echo.
echo Build completed successfully.
echo Output:
echo %CD%\dist\QBO Extension Apps

pause