# CTR Native Expanded Chaos Voting Client

Desktop client for choosing Chaos effects through Twitch chat.
The project is in its initial development phase.

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

## Current scope

The project currently contains a platform-independent voting round, a game
file adapter, and their tests. Session orchestration, Twitch integration,
the desktop UI, and the optional OBS overlay are not implemented yet.

The game protocol is documented in
[Chaos external file provider](https://github.com/thecodingbob/ctr-native/blob/master/docs/features/CHAOS_EXTERNAL_PROVIDER.md).
