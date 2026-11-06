# Project guidance

## Project and references

- This is a Windows desktop client for Twitch chat voting on CTR Native Expanded
  Chaos effects. The implementation is still in progress; see `README.md` for
  current scope and `docs/CLIENT_PLAN.md` for requirements and the roadmap.
- The authoritative game file protocol is
  https://github.com/thecodingbob/ctr-native/blob/master/docs/features/CHAOS_EXTERNAL_PROVIDER.md.
  Check the current Twitch developer documentation when implementing Twitch APIs.
- Keep the README and plan consistent with changes to capabilities or decisions.

## Architecture

- `chaos_voting/voting.py`: platform-independent round rules, with injectable
  clock and randomness. Keep the rules independent of Twitch, OBS, and game files.
- `chaos_voting/game_files.py`: `GameProvider` contract and `GameFiles` adapter.
- `chaos_voting/platform.py`: `VotingPlatform` and `PlatformError` contracts.
  `chaos_voting/twitch.py` implements chat announcements through Twitch Helix;
  `chaos_voting/twitch_auth.py` implements public-client device authorization.
- `chaos_voting/session.py`: explicitly driven orchestration. The caller must
  serialize calls, schedule ticks, forward votes, and report every interruption
  in message collection, even if reconnection occurs between ticks.
- Keep adapters replaceable, and put shared contracts outside the session rather
  than declaring them in the orchestrator. Use simulated providers and injectable
  dependencies for tests.

## Logging and security

- Add useful logs for lifecycle transitions and failures when adding features;
  avoid logging the same waiting state on every tick.
- Never log access tokens, refresh tokens, device codes, authorization headers,
  raw response bodies, chat messages, or voter IDs. Do not expose secrets through
  exception messages or object representations. Avoid logging raw exceptions
  from external services when they may contain sensitive data.
- Treat authentication tokens and authorization codes as sensitive. Keep the
  optional overlay local by default and do not expose credentials or controls.

## Development and verification

- Python 3.15 is required. On Windows, install development dependencies with
  `.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt`.
- Run `.\.venv\Scripts\python.exe -m ruff check .` and
  `.\.venv\Scripts\python.exe -m pytest` after changes. Prefer deterministic
  tests that do not require Twitch, the game, or OBS.

## Coding Guidelines

- Prioritize readability and keep the code understandable; split functions
  that become too long, but do not add unnecessary abstractions.
- Use named constants and enums instead of unexplained magic numbers.
- Fix underlying issues rather than reverting a requested feature when an
  initial approach fails.
- Keep Twitch, game-file, and other integration-specific details behind their
  respective adapters; keep voting rules independent of those integrations.
- Avoid redundant parentheses and follow the surrounding Python style.
- Keep comments concise; do not overexplain or repeat the same explanation.
- The main branch is protected and no work should be directly done on it. Create feature branches and ask when in doubt.
- Do not reinvent the wheel: try to reuse code whenever possible.
- Prompt the user before adding a new dependency to the project, explaining why and how would it help.
- When you're done implementing something, spawn a sub-agent and ask it to do a code review.
- Always stage the files you create modify, unless asked differently.
