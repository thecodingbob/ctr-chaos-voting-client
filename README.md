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

The voting and session modules live in `chaos_voting/`; game and Twitch
integrations live in `chaos_voting/game/` and `chaos_voting/twitch/`.
Tests mirror this layout under `tests/`.

The project currently contains a platform-independent voting round, a game
file adapter, and explicitly driven session orchestration with tests. The
session requires a platform adapter to forward connection changes and votes,
and a caller to schedule ticks. The Twitch adapter can send announcements via
the Helix API. A public-client device authorization flow obtains and validates
tokens in memory and supports explicit token refresh; token storage, automatic
refresh and periodic validation are not yet implemented. An explicitly driven
EventSub receiver can subscribe and forward chat events. A blocking runner
connects it to the session, drains notifications before ticks, and retries
connections until asked to stop.
The application still needs to manage the runner and expose its status safely.
The session filters EventSub notification timestamps before round opening and
deduplicates IDs across two rounds.
Twitch's notification timestamp is not necessarily the chat publication time.
Votes processed after the monotonic deadline are ignored, and processing order
may not match chat publication order. The
desktop UI and optional OBS overlay are not implemented yet. The client is not
yet usable end-to-end with Twitch.

The game protocol is documented in
[Chaos external file provider](https://github.com/thecodingbob/ctr-native/blob/master/docs/features/CHAOS_EXTERNAL_PROVIDER.md).
