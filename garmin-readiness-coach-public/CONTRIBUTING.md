# Contributing

Thanks for helping! A few ground rules keep this project safe for everyone's health data.

## Ground rules

- **No real personal data, anywhere.** Not in code, tests, fixtures, screenshots, issues or commit
  messages. Use demo mode (`GARMIN_DEMO=1`) and invented values. Test data should look obviously
  fake ("Test Person", 555-01xx phone numbers, round dates).
- **No keys.** `.env` is git-ignored. Only `.env.example` is committed, and it stays empty. If you add
  an environment variable, add it there with a comment (a test checks this).
- **Personal use only.** Don't add features that turn the app into a hosted or multi-user service, or
  that collect other people's Garmin data. Garmin's Personal API is for each user's own data.
- **No Garmin, Anthropic or BodySpec logos or branding.** Naming them to say what data the app works
  with is fine.
- **Nothing new leaves the machine silently.** A feature that sends data anywhere must be opt-in, go
  through `privacy.py` if it reaches an AI model, and be listed in the README's privacy table.
- Don't weaken the local-server guard (Host/Origin checks, no CORS, `/api/raw` off by default).

## Development setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v   # all tests must pass
GARMIN_DEMO=1 .venv/bin/python run.py --browser
```

Tests use throwaway data folders and never call Garmin or Anthropic. For the DEXA parser, change
`tests/fixtures/synthetic_dexa.py` (all values invented), then regenerate the PDF:
`.venv/bin/python tests/fixtures/synthetic_dexa.py`. To check against your own BodySpec reports, put
them in `samples/` (git-ignored). The optional test there checks only structure and consistency.
Never copy values from a real report into a test.

## Pull requests

1. Keep changes focused. Match the surrounding code's style.
2. Add or update tests for behavior changes.
3. If you add a dependency, add it to `requirements.txt` with its license in `THIRD_PARTY_NOTICES.md`.
   Front-end libraries must be version-pinned with an SRI hash (`tests/test_frontend_sri.py`).
4. Before pushing, check that nothing personal is staged: `git status`, `git diff --cached`, and
   ideally [`gitleaks`](https://github.com/gitleaks/gitleaks) `git --staged`.

By contributing, you agree your contribution is licensed under the [MIT License](LICENSE).
