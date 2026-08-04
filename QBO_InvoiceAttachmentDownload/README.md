# QBO Extension Apps UI

A beginner-friendly desktop UI shell for QuickBooks Online automation tools.

## Install

Open Command Prompt in this folder and run:

```cmd
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

## Included

- Sidebar navigation
- QuickBooks connection screen
- Divvy upload wizard
- File and folder selectors
- Review screen
- Background processing thread
- Progress bar and activity log
- Settings saved under `%APPDATA%\QBOExtensionApps`
- Placeholder pages for Lyft and invoice workflows

## Important

The QuickBooks connection is currently a demonstration screen. Replace
`ConnectionPage.connect_qbo()` with the Intuit OAuth 2.0 authorization flow.

The Divvy upload currently simulates processing. Replace `demo_worker()` inside
`DivvyWizardPage.start_demo_upload()` with your existing receipt processing
function.

Do not commit these files:

```gitignore
.vscode/
DivvyReceiptsUploadDev/
QBO.env
.venv/
__pycache__/
*.pyc
```
