"""The only place that talks to Anthropic: the official SDK, model routing, prompt caching, the
tool-use loop, structured output with validation, the output cache, usage/cost logging and the
monthly spending cap.

Prompt caching: tools and the stable system blocks (instructions, then your profile and coach notes)
come first and carry cache breakpoints; everything that changes per request (the day's data, the
question) comes after, in the user message. Hits show up as `cache_read_input_tokens` in the usage
log. The output cache keys every result by a hash of its inputs, so unchanged data never calls the
API again. Keys are read by the SDK from the environment and never logged (applog redacts them).
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from datetime import date
from typing import Callable

import anthropic

import health
import store

DEFAULTS = {"deep": "claude-sonnet-5-5", "fast": "claude-haiku-5-5"}
# $ per million tokens: input, output, 5-minute cache write, cache read (platform.claude.com pricing, 2026-10-29)
PRICES = {
    "claude-opus-5-5": (4.0, 20.0, 5.0, 0.20),
    "claude-sonnet-5-5": (2.0, 10.0, 2.50, 0.10),
    "claude-haiku-5-5": (0.10, 0.50, 0.125, 0.01),  # prompts up to 100K tokens (ours are far below)
    "claude-fable-5-1": (10.0, 50.0, 12.50, 0.25),
}
MODEL_CHOICES = ("claude-sonnet-5-5", "claude-haiku-5-5", "claude-opus-5-5")
# feature -> (tier, effort). Fast: short summaries and one-line takeaways; deep: reviews, analyses, chat.
ROUTES = {
    "briefing": ("deep", "medium"), "chat": ("deep", "medium"), "activity": ("deep", "medium"),
    "dexa": ("deep", "medium"), "dexa_trend": ("deep", "medium"), "review": ("deep", "medium"),
    "goal_plan": ("deep", "medium"), "goal_suggest": ("fast", "low"), "plan_overview": ("fast", "low"),
    "insights": ("fast", "low"), "test": ("fast", "low"), "thread": ("deep", "medium"), "summary": ("fast", "low"),
    "notice": ("fast", "low"),
}
FEATURE_LABELS = {
    "briefing": "Daily briefing", "chat": "Ask the coach", "activity": "Workout analysis", "dexa": "DEXA analysis",
    "dexa_trend": "DEXA trend", "review": "Weekly review", "goal_plan": "Goal plan", "goal_suggest": "Goal ideas",
    "plan_overview": "Plan notes", "insights": "Insights explanation", "test": "Connection test", "thread": "Coach conversation",
    "summary": "Conversation summary", "notice": "Coach noticed",
}
# Server-side refusal fallback (beta) is offered on these models; on a refusal elsewhere the rules take over.
FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"
PROMPT_VERSION = 2   # bump when prompts or schemas change, so cached outputs are rebuilt
MAX_TOKENS = 16000   # thinking counts against this; the visible answers are short
MAX_STEPS = 6        # model requests per tool loop
DEADLINE_S = 90.0    # wall-clock budget for one tool loop
TIMEOUT_S = 60.0     # per request (the SDK retries 408/409/429/5xx with backoff, honoring retry-after)
MAX_RETRIES = 3
BACKOFF_S = 30 * 60  # an automatic call that just failed isn't retried for this long (each page open would pay again)

_client_lock = threading.Lock()
_client: tuple[str, anthropic.Anthropic] | None = None
make_client: Callable[[str], anthropic.Anthropic] = lambda key: anthropic.Anthropic(
    api_key=key, max_retries=MAX_RETRIES, timeout=anthropic.Timeout(TIMEOUT_S, connect=10.0))


class AIError(RuntimeError):
    """A failed or skipped AI call, with a plain-language `info` dict (kind, title, explain, fix)."""

    def __init__(self, info: dict):
        super().__init__(info.get("title") or info.get("kind"))
        self.info = info
        self.kind = info.get("kind")


def enabled() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY"))


def client() -> anthropic.Anthropic:
    global _client
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        raise AIError(dict(kind="no_key", title="No Anthropic API key", explain="ANTHROPIC_API_KEY isn't set.",
                           fix="Put it in .env as ANTHROPIC_API_KEY and restart."))
    with _client_lock:
        if not _client or _client[0] != hashlib.sha256(key.encode()).hexdigest():
            _client = (hashlib.sha256(key.encode()).hexdigest(), make_client(key))
        return _client[1]


# --------------------------------- settings ---------------------------------- #
def models() -> dict:
    """{deep, fast}: Settings first, then .env (COACH_MODEL_DEEP/FAST; the old COACH_MODEL = deep), then defaults."""
    s = store.settings()
    return dict(deep=s.get("ai_model_deep") or os.getenv("COACH_MODEL_DEEP") or os.getenv("COACH_MODEL") or DEFAULTS["deep"],
                fast=s.get("ai_model_fast") or os.getenv("COACH_MODEL_FAST") or DEFAULTS["fast"])


def routes() -> dict:
    """feature -> tier, with your overrides from Settings."""
    over = store.settings().get("ai_routes") or {}
    return {f: over.get(f) if over.get(f) in ("fast", "deep") else t for f, (t, _) in ROUTES.items()}


def route(feature: str) -> tuple[str, str]:
    """(model, effort) for a feature."""
    tier = routes().get(feature, "deep")
    return models()[tier], ROUTES.get(feature, ("deep", "medium"))[1]


def cap_usd() -> float | None:
    v = store.settings().get("ai_cap_usd")
    return float(v) if isinstance(v, (int, float)) and v > 0 else None


def month_start(today: date | None = None) -> str:
    return (today or date.today()).replace(day=1).isoformat()


def month_spend(today: date | None = None) -> float:
    return round(sum(r["cost_usd"] or 0 for r in store.ai_usage(month_start(today))), 4)


def cap_reached() -> bool:
    c = cap_usd()
    return c is not None and month_spend() >= c


# --------------------------------- cost / log -------------------------------- #
def cost(model: str, u: dict) -> float | None:
    p = PRICES.get(model)
    if not p:
        return None
    return round((u["input"] * p[0] + u["output"] * p[1] + u["cache_write"] * p[2] + u["cache_read"] * p[3]) / 1e6, 6)


def _add_usage(tot: dict, usage) -> None:
    tot["input"] += getattr(usage, "input_tokens", 0) or 0
    tot["output"] += getattr(usage, "output_tokens", 0) or 0
    tot["cache_read"] += getattr(usage, "cache_read_input_tokens", 0) or 0
    tot["cache_write"] += getattr(usage, "cache_creation_input_tokens", 0) or 0


def _log(feature: str, model: str, tot: dict, steps: int, ok: bool, kind: str | None) -> dict:
    row = dict(ts=time.strftime("%Y-%m-%dT%H:%M:%S"), day=date.today().isoformat(), feature=feature, model=model, steps=steps,
               ok=int(ok), error_kind=kind, cost_usd=cost(model, tot), **tot)
    try:
        store.ai_usage_add(row)
    except Exception:  # the log must never break a coach answer
        pass
    return row


def classify(ex: BaseException) -> dict:
    """An SDK exception in plain words (the same kinds the System status panel already explains)."""
    if isinstance(ex, AIError):
        return ex.info
    if isinstance(ex, anthropic.APITimeoutError):
        return dict(kind="timeout", title="The AI service took too long", explain="No reply before the time limit, even after retrying.",
                    fix="Try again in a minute.")
    if isinstance(ex, anthropic.APIConnectionError):
        return dict(kind="network", title="Couldn't reach Anthropic", explain="The connection failed.",
                    fix="Check that this computer is online, then retry.")
    if isinstance(ex, anthropic.APIStatusError):
        body = ex.body if isinstance(ex.body, dict) else {}
        return health.classify_ai(ex.status_code, json.dumps(body))
    return dict(kind="error", title="The AI call failed", explain=type(ex).__name__, fix="Try again; the details are in the log.")


# ---------------------------------- requests --------------------------------- #
def _system(blocks: list[str]) -> list[dict]:
    """Stable system blocks with a cache breakpoint after each (instructions, then profile + notes), so
    a change to your notes keeps the cached instructions."""
    out = [dict(type="text", text=b) for b in blocks if b]
    for b in out[-2:]:
        b["cache_control"] = {"type": "ephemeral"}
    return out


def _tools(tools: list[dict] | None) -> list[dict] | None:
    if not tools:
        return None
    out = [dict(t, strict=True) for t in tools]
    out[-1] = dict(out[-1], cache_control={"type": "ephemeral"})
    return out


def _create(model: str, **kw):
    c = client()
    if model in FALLBACK_MODELS:  # on a policy decline the API re-runs the request on a fallback model
        return c.beta.messages.create(model=model, betas=[FALLBACK_BETA], fallbacks="default", **kw)
    return c.messages.create(model=model, **kw)


def _text(r) -> str:
    return "".join(b.text for b in r.content if getattr(b, "type", None) == "text").strip()


def call(feature: str, system: list[str], user: str, *, schema: dict | None = None, validate: Callable | None = None,
         tools: list[dict] | None = None, run_tool: Callable[[str, dict], dict] | None = None, essential: bool = True,
         max_steps: int = MAX_STEPS, deadline_s: float = DEADLINE_S) -> dict:
    """One feature call: a single request, or the tool loop when `tools` are given. Returns
    {data|text, model, usage, steps, cost_usd}. Raises AIError (no key, cap reached, API error,
    refusal, malformed output) - callers fall back to the rules."""
    if not enabled():
        client()  # raises the no-key AIError
    if not essential and cap_reached():
        raise AIError(dict(kind="cap", title="Monthly AI budget reached",
                           explain=f"This month's estimated spend reached your ${cap_usd():.2f} cap, so automatic calls are paused.",
                           fix="Raise or clear the cap in Settings › Coach, or use Regenerate to run it anyway."))
    model, effort = route(feature)
    fkey = hashlib.sha256(json.dumps([feature, model, system, user], default=str).encode()).hexdigest()[:24]
    if not essential and time.time() - _failed.get(fkey, 0) < BACKOFF_S:
        raise AIError(dict(kind="backoff", title="Waiting before retrying the coach",
                           explain="The last automatic attempt with this data failed, so it isn't retried for 30 minutes.",
                           fix="Use Regenerate to try now."))
    oc: dict = dict(effort=effort)
    if schema:
        oc["format"] = dict(type="json_schema", schema=schema)
    kw = dict(max_tokens=MAX_TOKENS, system=_system(system), output_config=oc)
    tl = _tools(tools)
    if tl:
        kw["tools"] = tl
    messages: list = [dict(role="user", content=user)]
    tot = dict(input=0, output=0, cache_read=0, cache_write=0)
    t0, steps, repaired = time.monotonic(), 0, False
    try:
        while True:
            steps += 1
            last = steps >= max_steps or time.monotonic() - t0 > deadline_s
            extra = {"tool_choice": {"type": "none"}} if tl and last else {}  # out of budget: answer from what it has
            r = _create(model, messages=messages, **kw, **extra)
            _add_usage(tot, r.usage)
            if r.stop_reason == "tool_use" and not last:
                messages.append(dict(role="assistant", content=r.content))
                results = []
                for b in r.content:
                    if getattr(b, "type", None) != "tool_use":
                        continue
                    res = run_tool(b.name, b.input) if run_tool else dict(error="No tools are available here.")
                    results.append(dict(type="tool_result", tool_use_id=b.id, content=json.dumps(res, default=str),
                                        **({"is_error": True} if "error" in res else {})))
                messages.append(dict(role="user", content=results))
                continue
            if r.stop_reason == "refusal":
                raise AIError(dict(kind="refusal", title="The coach declined to answer",
                                   explain="Claude's safety checks declined this request.", fix="Rephrase it, or use the rule-based coach."))
            if r.stop_reason == "max_tokens":
                raise AIError(dict(kind="truncated", title="The answer was cut off", explain="It ran past the length limit.",
                                   fix="Try again."))
            text = _text(r)
            if not text:
                raise AIError(dict(kind="empty", title="The coach returned nothing", explain=f"stop_reason={r.stop_reason}",
                                   fix="Try again."))
            if not schema:
                out = dict(text=text)
                break
            try:
                data = json.loads(text)
                problem = validate(data) if validate else None
            except ValueError as ex:
                data, problem = None, f"not valid JSON ({ex})"
            if not problem:
                out = dict(data=data)
                break
            if repaired or steps >= max_steps + 1:
                raise AIError(dict(kind="malformed", title="The coach's answer didn't match the expected format",
                                   explain=str(problem)[:200], fix="Showing the rule-based version instead."))
            repaired = True  # one repair round: show the problem, ask for corrected JSON only
            messages += [dict(role="assistant", content=r.content),
                         dict(role="user", content=f"That JSON failed validation: {problem}. Reply with corrected JSON only.")]
    except AIError as ex:
        _failed[fkey] = time.time()
        _log(feature, model, tot, steps, False, ex.kind)
        health.event("warning" if ex.kind in ("refusal", "malformed", "truncated", "cap") else "error", "AI coach",
                     f"{FEATURE_LABELS.get(feature, feature)}: {ex.info['title']}")
        _note_error(ex.info)
        raise
    except Exception as ex:
        _failed[fkey] = time.time()
        info = classify(ex)
        _log(feature, model, tot, steps, False, info["kind"])
        health.event("error", "AI coach", f"{FEATURE_LABELS.get(feature, feature)}: {info['title']}")
        _note_error(info)
        raise AIError(info) from ex
    _failed.pop(fkey, None)
    row = _log(feature, model, tot, steps, True, None)
    _note_ok(model)
    return dict(out, model=model, usage=tot, steps=steps, cost_usd=row["cost_usd"])


# ---------------------------------- streaming --------------------------------- #
def _stream(model: str, **kw):
    c = client()
    if model in FALLBACK_MODELS:
        return c.beta.messages.stream(model=model, betas=[FALLBACK_BETA], fallbacks="default", **kw)
    return c.messages.stream(model=model, **kw)


def stream(feature: str, system: list[str], messages: list[dict], *, tools: list[dict] | None = None,
           run_tool: Callable[[str, dict], dict] | None = None, essential: bool = True, max_steps: int = MAX_STEPS,
           deadline_s: float = DEADLINE_S):
    """The tool loop, streamed: yields ("text", delta), ("tool", name) while it runs, then ("done", {text,
    model, usage, steps, cost_usd}). Same budget, backoff, caching and usage log as call(). Raises AIError."""
    if not enabled():
        client()
    if not essential and cap_reached():
        raise AIError(dict(kind="cap", title="Monthly AI budget reached", explain="Automatic calls are paused.",
                           fix="Raise or clear the cap in Settings › Coach."))
    model, effort = route(feature)
    fkey = hashlib.sha256(json.dumps([feature, model, system, messages], default=str).encode()).hexdigest()[:24]
    kw = dict(max_tokens=MAX_TOKENS, system=_system(system), output_config=dict(effort=effort),
              cache_control={"type": "ephemeral"})  # also cache the growing conversation between steps and turns
    tl = _tools(tools)
    if tl:
        kw["tools"] = tl
    msgs = list(messages)
    tot = dict(input=0, output=0, cache_read=0, cache_write=0)
    t0, steps, parts = time.monotonic(), 0, []
    try:
        while True:
            steps += 1
            last = steps >= max_steps or time.monotonic() - t0 > deadline_s
            extra = {"tool_choice": {"type": "none"}} if tl and last else {}
            first = True
            with _stream(model, messages=msgs, **kw, **extra) as st:
                for ev in st:
                    if getattr(ev, "type", None) == "text" and ev.text:
                        if first and parts and not parts[-1].endswith("\n\n"):
                            parts.append("\n\n")
                            yield ("text", "\n\n")
                        first = False
                        parts.append(ev.text)
                        yield ("text", ev.text)
                r = st.get_final_message()
            _add_usage(tot, r.usage)
            if r.stop_reason == "tool_use" and not last:
                msgs.append(dict(role="assistant", content=r.content))
                results = []
                for b in r.content:
                    if getattr(b, "type", None) != "tool_use":
                        continue
                    yield ("tool", b.name)
                    res = run_tool(b.name, b.input) if run_tool else dict(error="No tools are available here.")
                    results.append(dict(type="tool_result", tool_use_id=b.id, content=json.dumps(res, default=str),
                                        **({"is_error": True} if "error" in res else {})))
                msgs.append(dict(role="user", content=results))
                continue
            if r.stop_reason == "refusal":
                raise AIError(dict(kind="refusal", title="The coach declined to answer",
                                   explain="Claude's safety checks declined this request.", fix="Rephrase it."))
            if r.stop_reason == "max_tokens":
                raise AIError(dict(kind="truncated", title="The answer was cut off", explain="It ran past the length limit.",
                                   fix="Try again."))
            break
        text = "".join(parts).strip()
        if not text:
            raise AIError(dict(kind="empty", title="The coach returned nothing", explain=f"stop_reason={r.stop_reason}", fix="Try again."))
    except AIError as ex:
        _failed[fkey] = time.time()
        _log(feature, model, tot, steps, False, ex.kind)
        health.event("warning" if ex.kind in ("refusal", "truncated", "cap", "empty") else "error", "AI coach",
                     f"{FEATURE_LABELS.get(feature, feature)}: {ex.info['title']}")
        _note_error(ex.info)
        raise
    except Exception as ex:
        _failed[fkey] = time.time()
        info = classify(ex)
        _log(feature, model, tot, steps, False, info["kind"])
        health.event("error", "AI coach", f"{FEATURE_LABELS.get(feature, feature)}: {info['title']}")
        _note_error(info)
        raise AIError(info) from ex
    row = _log(feature, model, tot, steps, True, None)
    _note_ok(model)
    yield ("done", dict(text=text, model=model, usage=tot, steps=steps, cost_usd=row["cost_usd"]))


# ------------------------------- status tracking ----------------------------- #
_last = dict(ok=None, error=None, at=None, model=None)
_failed: dict[str, float] = {}  # request hash -> time of the last failure


def _note_error(info: dict) -> None:
    if info.get("kind") in ("cap", "refusal", "malformed", "truncated", "empty"):
        return  # not a connection problem: the panel shows these through the usage log
    _last.update(ok=False, error={k: info.get(k) for k in ("kind", "title", "explain", "fix")}, at=time.time())


def _note_ok(model: str) -> None:
    _last.update(ok=True, error=None, at=time.time(), model=model)


def last_status() -> dict:
    return dict(_last)


def ping() -> dict:
    """The smallest real request (System status › Test): confirms the key works and the model answers."""
    model = route("test")[0]
    t0 = time.time()
    r = _create(model, max_tokens=256, output_config=dict(effort="low"),
                messages=[dict(role="user", content="Reply with the single word OK.")])
    tot = dict(input=0, output=0, cache_read=0, cache_write=0)
    _add_usage(tot, r.usage)
    _log("test", model, tot, 1, True, None)
    _note_ok(model)
    return dict(model=getattr(r, "model", None) or model, ms=round((time.time() - t0) * 1000))


# --------------------------------- output cache ------------------------------- #
def cache_key(feature: str, inputs, memory: str = "", extra=None) -> str:
    """Hash of everything that shapes the answer: feature, model, effort, prompt version, the inputs
    (canonical JSON), your profile + notes and any extra (units, sharing...)."""
    model, effort = route(feature)
    blob = json.dumps(dict(f=feature, m=model, e=effort, v=PROMPT_VERSION, i=inputs, mem=memory, x=extra),
                      sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def cached(feature: str, key: str, produce: Callable[[], dict], refresh: bool = False) -> dict:
    """The stored result for `key`, or produce() (and store it). Only successful AI results are stored."""
    if not refresh:
        hit = store.ai_cache_get(key)
        if hit is not None:
            return dict(hit, cached=True)
    res = produce()
    if res.get("source") == "claude":
        store.ai_cache_put(key, feature, res)
    return res


# ----------------------------------- summary ----------------------------------- #
def usage_summary(today: date | None = None) -> dict:
    """This month's calls, tokens, cache hit rate and estimated cost, by feature and model."""
    today = today or date.today()
    rows = store.ai_usage(month_start(today))
    tot = dict(calls=len(rows), failed=sum(1 for r in rows if not r["ok"]), input=0, output=0, cache_read=0, cache_write=0,
               cost_usd=0.0, unknown_cost=0)
    by_feature: dict = {}
    for r in rows:
        for k in ("input", "output", "cache_read", "cache_write"):
            tot[k] += r[k]
        if r["cost_usd"] is None:
            tot["unknown_cost"] += 1
        tot["cost_usd"] += r["cost_usd"] or 0
        f = by_feature.setdefault(r["feature"], dict(feature=r["feature"], label=FEATURE_LABELS.get(r["feature"], r["feature"]),
                                                     calls=0, cost_usd=0.0, input=0, output=0, cache_read=0))
        f["calls"] += 1
        f["cost_usd"] += r["cost_usd"] or 0
        for k in ("input", "output", "cache_read"):
            f[k] += r[k]
    prompt = tot["input"] + tot["cache_read"] + tot["cache_write"]
    tot["cache_hit_pct"] = round(100 * tot["cache_read"] / prompt) if prompt else None
    tot["cost_usd"] = round(tot["cost_usd"], 4)
    for f in by_feature.values():
        f["cost_usd"] = round(f["cost_usd"], 4)
    cap = cap_usd()
    return dict(month=month_start(today)[:7], totals=tot, by_feature=sorted(by_feature.values(), key=lambda x: -x["cost_usd"]),
                cap_usd=cap, cap_reached=cap is not None and tot["cost_usd"] >= cap, models=models(), routes=routes(),
                key_set=enabled(), last=last_status(), recent=[{k: r[k] for k in ("ts", "feature", "model", "input", "output",
                                                                                     "cache_read", "cache_write", "cost_usd", "ok", "error_kind", "steps")}
                                                              for r in rows[-8:][::-1]])
