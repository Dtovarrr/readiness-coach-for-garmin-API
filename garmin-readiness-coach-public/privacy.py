"""The one filter every piece of data passes through before it can reach Claude.

Derived numbers only: names, emails, birth dates, GPS and location fields, photos, file paths and
free-text notes are dropped by key wherever they appear, and every remaining string goes through
`health.redact_text` (keys, tokens, emails, coordinates, birth dates). Garmin activity IDs never
leave either: tools hand the model short refs ("a1", "a2"...) that map back to IDs only here.
"""
from __future__ import annotations

import re

import health

DROP_KEYS = re.compile(
    r"(?i)^(name|names|activity_?name|display_?name|full_?name|user_?name|first_?name|last_?name|owner\w*|email|e_?mail|"
    r"birth\w*|dob|date_?of_?birth|age_?exact|lat|lon|lng|latitude|longitude|\w*_lat|\w*_lon|\w*latitude|\w*longitude|"
    r"position_\w+|gps\w*|route|polyline|track|location\w*|city|address|place|photo\w*|caption|thumb|path|pdf|file\w*|"
    r"note|notes|comment|comments|description|text_note|raw\w*|report_text|client\w*|id|activity_?id|\w+_id|uuid)$")


def scrub(obj, keep: frozenset = frozenset()):
    """A copy with sensitive keys removed and every string redacted. `keep` names keys that are safe in
    this payload even though they match (e.g. a tool's own opaque "ref")."""
    if isinstance(obj, dict):
        return {k: scrub(v, keep) for k, v in obj.items() if k in keep or not DROP_KEYS.match(str(k))}
    if isinstance(obj, (list, tuple)):
        return [scrub(x, keep) for x in obj]
    if isinstance(obj, str):
        return health.redact_text(obj)
    return obj


class Refs:
    """Opaque per-conversation handles for activity IDs ("a1", "a2"...). The model only ever sees refs."""

    def __init__(self):
        self._by_id: dict = {}
        self._by_ref: dict = {}

    def ref(self, aid) -> str:
        if aid not in self._by_id:
            r = f"a{len(self._by_id) + 1}"
            self._by_id[aid], self._by_ref[r] = r, aid
        return self._by_id[aid]

    def resolve(self, ref: str):
        return self._by_ref.get(ref)
