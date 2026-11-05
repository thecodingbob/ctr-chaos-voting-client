# CTR Native Expanded Chaos Voting Client

Desktop client for choosing Chaos effects through Twitch chat.
The project is in its initial setup phase; the client is not implemented yet.

## Development environment

Python 3.15 is required. In PowerShell, from the project directory:

```powershell
py -3.15 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

Run checks and tests with:

```powershell
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m pytest
```

Development dependencies use version ranges, not a lockfile: separate
installations may use different compatible versions.
