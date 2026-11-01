# Security policy

## Never post secrets or personal data

This app handles API keys and personal health data. **Never put any of the following in a GitHub
issue, pull request, discussion, screenshot or log excerpt:**

- `GARMIN_API_KEY`, `ANTHROPIC_API_KEY`, `INTERVALS_API_KEY`, access tokens, or anything after
  `Authorization:`, `Bearer ` or `ApiKey `
- your `.env` file, or a private calendar (`COACH_CALENDAR_ICS`) URL
- files from `.data/`, `.cache/`, `logs/` or `samples/`, export zips, DEXA PDFs or progress photos
- your name, email, birth date, home city or address, or GPS routes
- health values you'd rather not publish (weight, body fat, HRV, scan results)

If you post a key by accident, **revoke it right away** (delete it on Garmin's Personal API Key page
or in the Anthropic Console, then create a new one). Editing or deleting the post isn't enough:
content can be cached, emailed to watchers or indexed. Then let the maintainers know so the post can
be removed.

The diagnostics report (**Settings > System status**) and `logs/readiness_coach.log` redact keys
and tokens, but **check them yourself before sharing** and remove anything personal.

## Reporting a vulnerability

Please **don't open a public issue** for a security problem. Use GitHub's private
[security advisory form](../../security/advisories/new) (Security tab > "Report a vulnerability").
Include:

- what's affected (file/route) and the impact
- steps to reproduce, using demo mode (`GARMIN_DEMO=1`) or made-up data, never your real keys or data
- the version or commit

We aim to acknowledge reports within a week. This is a volunteer project with no bug bounty.

## Scope and threat model

The app is meant to run on your own computer for one person. It binds to `127.0.0.1`, validates the
`Host` header, requires a same-origin `Origin`/`Referer` on every state-changing request, and sends
no CORS headers. In scope: anything that lets another website, another user on the network, or a
malicious file (PDF, photo, ICS, FIT) read or change your data or keys, run code, or make the app
send data somewhere it shouldn't.

Out of scope: someone who already controls your user account or your `.env`, running the server on a
public interface (unsupported), and issues in Garmin's, Anthropic's or other services' own APIs.
Report those to the provider.
