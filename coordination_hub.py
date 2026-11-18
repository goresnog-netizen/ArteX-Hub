#!/usr/bin/env python3
"""Multi-provider A2A coordination hub for the snapgent bridge.

ProviderManager  - registry, quota tracking (requests/min, tokens/min,
                   concurrent), health tracking, auto-switch on exhaustion
                   or failure, latency + priority scoring.
MemoryOptimizer  - per-provider context-window strategies:
                     chatgpt  -> sliding-window (keep tail)
                     deepseek -> append-stable (KV-cache friendly, stable prefix)
                     gemini   -> hierarchical (compressed middle, keep head/tail)
                     qwen     -> rolling-summary (summary block + priority tail)
A2AProtocol      - agent-to-agent message envelope, routing, broadcast.

Pure stdlib. No network calls at import time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set

import json
import os
import time
import uuid


# --------------------------------------------------------------------------
# Exceptions
# --------------------------------------------------------------------------


class AllProvidersExhausted(Exception):
    """Raised when get_best_provider finds no provider with available quota."""

    pass


# --------------------------------------------------------------------------
# ProviderConfig
# --------------------------------------------------------------------------


@dataclass
class ProviderConfig:
    endpoint: str
    api_key: str = ""
    models: List[str] | None = None
    requests_per_minute: int | None = None
    tokens_per_minute: int | None = None
    max_concurrent: int | None = None
    transform_request: Callable[[Dict[str, Any]], Dict[str, Any]] | None = None
    transform_response: Callable[[Dict[str, Any]], Dict[str, Any]] | None = None
    auth: str = ""
    headers: Dict[str, str] = field(default_factory=dict)
    timeout: float | None = None
    max_retries: int = 3
    priority: int = 1


# Default on-disk location for the shared memory (cross-process, cross-provider).
SHARED_MEMORY_PATH = os.path.join("state", "shared_memory.json")


# --------------------------------------------------------------------------
# Hybrid dict / object access for usage and health records
# --------------------------------------------------------------------------


class _Record:
    """A dict that also supports attribute access: r.key and r[key] are equivalent."""

    __slots__ = ("_data",)

    def __init__(self, initial: Dict[str, Any] | None = None) -> None:
        object.__setattr__(self, "_data", dict(initial) if initial else {})

    def __getattr__(self, name: str) -> Any:
        try:
            return object.__getattribute__(self, "_data")[name]
        except KeyError:
            raise AttributeError(name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "_data":
            object.__setattr__(self, name, value)
        else:
            self._data[name] = value

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self._data[key] = value

    def __contains__(self, key: str) -> bool:
        return key in self._data

    def to_dict(self) -> Dict[str, Any]:
        return dict(self._data)


# --------------------------------------------------------------------------
# SharedMemory - one memory shared by every provider/agent
# --------------------------------------------------------------------------


class SharedMemory:
    """Task-keyed memory shared across all providers.

    short_term: rolling recent messages per task (what just happened)
    long_term : compacted summaries per task (what matters long-term)
    bootstrap : on any task, returns the last-work redirect + memory so
                every provider/agent resumes the same place.
    """

    DEFAULT_SHORT_LIMIT = 40
    DEFAULT_LONG_LIMIT = 20

    def __init__(
        self,
        state_path: Optional[str] = None,
        short_limit: int = DEFAULT_SHORT_LIMIT,
        long_limit: int = DEFAULT_LONG_LIMIT,
        auto_persist: bool = True,
    ) -> None:
        self.state_path = state_path
        self.short_limit = short_limit
        self.long_limit = long_limit
        self.auto_persist = auto_persist
        self.short_term: Dict[str, List[Dict[str, Any]]] = {}
        self.long_term: Dict[str, List[Dict[str, Any]]] = {}
        self.last_task: Optional[str] = None
        self.last_activity: float = 0.0
        if state_path:
            self.load()

    # -- persistence -------------------------------------------------------
    def load(self) -> None:
        if not self.state_path or not os.path.exists(self.state_path):
            return
        try:
            with open(self.state_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self.short_term = data.get("short_term", {})
            self.long_term = data.get("long_term", {})
            self.last_task = data.get("last_task")
            self.last_activity = data.get("last_activity", 0.0)
        except (OSError, ValueError):
            self.short_term, self.long_term = {}, {}
            self.last_task, self.last_activity = None, 0.0

    def persist(self) -> None:
        if not self.state_path:
            return
        try:
            os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
            with open(self.state_path, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "short_term": self.short_term,
                        "long_term": self.long_term,
                        "last_task": self.last_task,
                        "last_activity": self.last_activity,
                    },
                    fh,
                    ensure_ascii=False,
                    indent=2,
                )
        except OSError:
            pass  # memory keeps working even if disk write fails

    # -- write -------------------------------------------------------------
    def record(self, task: str, messages: List[Dict[str, Any]]) -> None:
        """Append messages to the shared short-term buffer for a task."""
        bucket = self.short_term.setdefault(task, [])
        for m in messages:
            entry = dict(m)
            entry.setdefault("timestamp", time.time())
            bucket.append(entry)
        if len(bucket) > self.short_limit:
            overflow = bucket[: len(bucket) - self.short_limit]
            del bucket[: len(bucket) - self.short_limit]
            self._promote(task, overflow)
        self.last_task = task
        self.last_activity = time.time()
        if self.auto_persist:
            self.persist()

    def _promote(self, task: str, overflow: List[Dict[str, Any]]) -> None:
        """Fold evicted short-term messages into the long-term summary."""
        long_bucket = self.long_term.setdefault(task, [])
        lines = []
        for m in overflow:
            role = m.get("role", "?")
            text = str(m.get("content", ""))[:160]
            lines.append("%s: %s" % (role, text))
        long_bucket.append(
            {
                "summary": " | ".join(lines),
                "count": len(overflow),
                "timestamp": time.time(),
            }
        )
        if len(long_bucket) > self.long_limit:
            del long_bucket[: len(long_bucket) - self.long_limit]

    def set_long_term(self, task: str, summary: str) -> None:
        """Inject an external (agent/LLM) long-term summary for a task."""
        self.long_term.setdefault(task, []).append(
            {"summary": summary, "count": 0, "timestamp": time.time()}
        )
        bucket = self.long_term[task]
        if len(bucket) > self.long_limit:
            del bucket[: len(bucket) - self.long_limit]
        if self.auto_persist:
            self.persist()

    # -- read --------------------------------------------------------------
    def get_short_term(self, task: str) -> List[Dict[str, Any]]:
        return list(self.short_term.get(task, []))

    def get_long_term(self, task: str) -> List[Dict[str, Any]]:
        return list(self.long_term.get(task, []))

    def _sanitize_untrusted(self, text: str) -> str:
        """Wrap untrusted content in delimiter, neutralise delimiter inside."""
        text = str(text)
        text = text.replace(
            "<<<PEER_FINDINGS_UNTRUSTED>>>", "___PEER_FINDINGS_UNTRUSTED___"
        )
        text = text.replace(
            "<<<END_PEER_FINDINGS_UNTRUSTED>>>", "___END_PEER_FINDINGS_UNTRUSTED___"
        )
        return (
            "<<<PEER_FINDINGS_UNTRUSTED>>>" + text + "<<<END_PEER_FINDINGS_UNTRUSTED>>>"
        )

    def bootstrap_messages(self, task: str) -> List[Dict[str, Any]]:
        """Messages every provider gets before its own context.

        1. last-work redirect (which task was active, so an agent that
           boots cold lands on the same place as one that was mid-task)
        2. long-term summary block
        3. short-term recent messages (shared, not per-provider)
        """
        out: List[Dict[str, Any]] = []
        if self.last_task and self.last_task != task:
            out.append(
                {
                    "role": "system",
                    "content": self._sanitize_untrusted(
                        "[shared-memory] last active work: task '%s'. "
                        "Continue from there unless this request is a new task."
                        % self.last_task
                    ),
                }
            )
        longs = self.get_long_term(task)
        if longs:
            joined = "\n".join("- %s" % e["summary"] for e in longs)
            out.append(
                {
                    "role": "system",
                    "content": self._sanitize_untrusted(
                        "[shared-memory long-term] task '%s':\n%s" % (task, joined)
                    ),
                }
            )
        short = self.get_short_term(task)
        if short:
            out.append(
                {
                    "role": "system",
                    "content": self._sanitize_untrusted(
                        "[shared-memory short-term] recent shared context:"
                    ),
                }
            )
            for entry in short:
                out.append(
                    {
                        "role": "system",
                        "content": self._sanitize_untrusted(
                            "[shared-memory] %s: %s"
                            % (entry.get("role", "user"), entry.get("content", ""))
                        ),
                    }
                )
        return out

    def prepare(
        self, task: str, messages: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Inject shared memory in front, then record the new messages."""
        merged = self.bootstrap_messages(task) + list(messages)
        self.record(task, messages)
        return merged

    def stats(self) -> Dict[str, Any]:
        return {
            "tasks": sorted(set(self.short_term) | set(self.long_term)),
            "last_task": self.last_task,
            "short_messages": sum(len(v) for v in self.short_term.values()),
            "long_summaries": sum(len(v) for v in self.long_term.values()),
            "state_path": self.state_path,
        }


# --------------------------------------------------------------------------
# ProviderManager
# --------------------------------------------------------------------------


class ProviderManager:
    def __init__(self) -> None:
        self.providers: Dict[str, ProviderConfig] = {}
        self.current_provider: Optional[str] = None
        self.usage: Dict[str, _Record] = {}
        self.quotas: Dict[str, _Record] = {}
        self.health: Dict[str, _Record] = {}
        self.callbacks: Set[Callable] = set()

    # -- registration ------------------------------------------------------
    def register_provider(self, name: str, config: ProviderConfig) -> None:
        self.providers[name] = config
        self.usage[name] = _Record(
            {
                "requests": 0,
                "tokens": 0,
                "errors": 0,
                "concurrent": 0,
                "last_reset": time.time(),
                "window_start": time.time(),
            }
        )
        self.quotas[name] = _Record(
            {
                "requests_per_minute": config.requests_per_minute or 60,
                "tokens_per_minute": config.tokens_per_minute or 150000,
                "max_concurrent": config.max_concurrent or 5,
                "priority": config.priority,
            }
        )
        self.health[name] = _Record(
            {
                "status": "healthy",
                "latency": 0,
                "consecutive_failures": 0,
            }
        )

    def register(self, name: str, config: ProviderConfig) -> None:
        """Alias for register_provider."""
        self.register_provider(name, config)

    def unregister(self, name: str) -> None:
        self.providers.pop(name, None)
        self.usage.pop(name, None)
        self.quotas.pop(name, None)
        self.health.pop(name, None)

    def list_providers(self) -> List[str]:
        return list(self.providers.keys())

    # -- health ------------------------------------------------------------
    def is_healthy(self, name: str) -> bool:
        h = self.health.get(name)
        if not h:
            return False
        if h.status == "down":
            return False
        if h.consecutive_failures >= 5:
            return False
        return True

    def report_health(self, name: str, ok: bool = True, latency_ms: float = 0) -> None:
        h = self.health.setdefault(
            name,
            _Record({"status": "healthy", "latency": 0, "consecutive_failures": 0}),
        )
        if ok:
            h.status = "healthy"
            h.consecutive_failures = 0
        else:
            h.consecutive_failures += 1
            if h.consecutive_failures >= 3:
                h.status = "down"
            else:
                h.status = "degraded"
        h.latency = latency_ms
        for cb in self.callbacks:
            try:
                cb(name, h.status)
            except Exception:
                pass

    # -- quota -------------------------------------------------------------
    def has_quota(self, name: str, est_tokens: int = 0) -> bool:
        rq = self.remaining_quota(name)
        return (
            rq["requests"] > 0 and rq["tokens"] >= est_tokens and rq["concurrent"] > 0
        )

    def remaining_quota(self, name: str) -> Dict[str, int]:
        u = self.usage.get(name)
        q = self.quotas.get(name)
        if not u or not q:
            return {"requests": 0, "tokens": 0, "concurrent": 0}
        self._refresh_window(name)
        used_r = u.requests
        used_t = u.tokens
        used_c = u.concurrent
        return {
            "requests": max(0, q.requests_per_minute - used_r),
            "tokens": max(0, q.tokens_per_minute - used_t),
            "concurrent": max(0, q.max_concurrent - used_c),
        }

    def _refresh_window(self, name: str) -> None:
        u = self.usage.get(name)
        if not u:
            return
        now = time.time()
        if now - u.window_start > 60:
            u.requests = 0
            u.tokens = 0
            u.concurrent = 0
            u.window_start = now

    def report_usage(self, name: str, requests: int = 0, tokens: int = 0) -> None:
        u = self.usage.setdefault(
            name,
            _Record(
                {
                    "requests": 0,
                    "tokens": 0,
                    "errors": 0,
                    "concurrent": 0,
                    "last_reset": time.time(),
                    "window_start": time.time(),
                }
            ),
        )
        u.requests += requests
        u.tokens += tokens
        self._refresh_window(name)

    def reset_failure_count(self, name: str) -> None:
        h = self.health.get(name)
        if h:
            h.consecutive_failures = 0
            h.status = "healthy"

    # -- request lifecycle -------------------------------------------------
    def begin_request(self, name: str) -> None:
        u = self.usage.setdefault(
            name,
            _Record(
                {
                    "requests": 0,
                    "tokens": 0,
                    "errors": 0,
                    "concurrent": 0,
                    "last_reset": time.time(),
                    "window_start": time.time(),
                }
            ),
        )
        self._refresh_window(name)
        u.concurrent += 1
        u.requests += 1

    def end_request(
        self, name: str, ok: bool = True, tokens_used: int = 0, latency_ms: float = 0
    ) -> None:
        u = self.usage.setdefault(
            name,
            _Record(
                {
                    "requests": 0,
                    "tokens": 0,
                    "errors": 0,
                    "concurrent": 0,
                    "last_reset": time.time(),
                    "window_start": time.time(),
                }
            ),
        )
        u.concurrent = max(0, u.concurrent - 1)
        u.tokens += tokens_used
        if not ok:
            u.errors += 1
        self._refresh_window(name)

    # -- selection ---------------------------------------------------------
    def good_provider(self) -> Optional[str]:
        """Return a healthy provider with quota, or None (no exception)."""
        candidates = [
            n for n in self.providers if self.is_healthy(n) and self.has_quota(n)
        ]
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda n: self.quotas.get(n, _Record({"priority": 0})).priority,
        )

    def get_best_provider(
        self, allow_over_quota: bool = False, est_tokens: int = 0
    ) -> str:
        """Return the name of the best available provider.

        Raises AllProvidersExhausted if none are usable.
        If allow_over_quota=True, exhausted-but-healthy providers are accepted.
        est_tokens: optional token estimate to use when evaluating quota headroom.
        """
        if allow_over_quota:
            candidates = [n for n in self.providers if self.is_healthy(n)]
        else:
            candidates = [
                n
                for n in self.providers
                if self.is_healthy(n) and self.has_quota(n, est_tokens=est_tokens)
            ]
        if not candidates:
            raise AllProvidersExhausted("no provider has quota")
        return max(
            candidates,
            key=lambda n: self.quotas.get(n, _Record({"priority": 0})).priority,
        )

    # -- stats ------------------------------------------------------------
    def stats(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for name in self.providers:
            h = self.health.get(name, _Record())
            u = self.usage.get(name, _Record())
            result[name] = {
                "health": h.status if hasattr(h, "status") else "unknown",
                "usage": u.to_dict() if hasattr(u, "to_dict") else dict(u),
            }
        return result


# --------------------------------------------------------------------------
# MemoryOptimizer - per-provider context-window strategies
# --------------------------------------------------------------------------


def _msg_len(message: Dict[str, Any]) -> int:
    """Character weight of a message content (the budget unit used here)."""
    content = message.get("content", "")
    if isinstance(content, str):
        return len(content)
    try:
        return len(json.dumps(content, ensure_ascii=False))
    except Exception:
        return len(str(content))


def _sliding(messages: List[Dict[str, Any]], budget: int) -> List[Dict[str, Any]]:
    """Keep the newest messages that fit budget, with the system head pinned."""
    if not messages:
        return []
    head: List[Dict[str, Any]] = []
    body = list(messages)
    if messages[0].get("role") == "system":
        head = [messages[0]]
        body = list(messages[1:])
    result: List[Dict[str, Any]] = list(head)
    total = sum(_msg_len(m) for m in head)
    for m in reversed(body):
        l = _msg_len(m)
        if total + l > budget and len(result) > len(head):
            break
        result.insert(len(head), m)
        total += l
    return result


def _append_stable(messages: List[Dict[str, Any]], budget: int) -> List[Dict[str, Any]]:
    """Keep a stable prefix, digest the middle, keep the newest tail."""
    if not messages:
        return []
    if len(messages) <= 2:
        return list(messages)
    head = list(messages[:2])  # system prompt + first body message stay verbatim
    body = list(messages[2:])
    head_total = sum(_msg_len(m) for m in head)
    tail_budget = max(0, budget - head_total - 128)
    tail: List[Dict[str, Any]] = []
    total = 0
    for m in reversed(body):
        l = _msg_len(m)
        if total + l > tail_budget and tail:
            break
        tail.insert(0, m)
        total += l
    dropped = body[: len(body) - len(tail)]
    digest = {
        "role": "system",
        "content": "[compressed %d messages]" % len(dropped),
    }
    return head + [digest] + tail


def _hierarchical(messages: List[Dict[str, Any]], budget: int) -> List[Dict[str, Any]]:
    """Keep head + compressed middle + tail."""
    if not messages:
        return []
    tail_size = max(1, budget // 3)
    head = list(messages[:1])  # always keep system prompt at minimum
    body = list(messages[1:])
    # Build tail: take from end while staying within budget
    tail: List[Dict[str, Any]] = []
    tail_total = 0
    for m in reversed(body):
        l = _msg_len(m)
        if tail_total + l > tail_size and tail:
            break
        tail.insert(0, m)
        tail_total += l
    middle_count = len(body) - len(tail)
    if middle_count > 0:
        summary = {
            "role": "system",
            "content": "[compressed %d messages]" % middle_count,
        }
    else:
        summary = {"role": "system", "content": ""}
    return head + [summary] + tail


def _rolling_summary(
    messages: List[Dict[str, Any]], budget: int
) -> List[Dict[str, Any]]:
    """Keep the existing summary block + a fresh digest + recent tail."""
    if not messages:
        return []
    head: List[Dict[str, Any]] = []
    body = list(messages)
    if messages[0].get("role") == "system":
        head = [messages[0]]  # existing summary block stays first and verbatim
        body = list(messages[1:])
    summary_chars = max(100, budget // 4)
    tail_budget = max(0, budget - summary_chars - 200)
    tail: List[Dict[str, Any]] = []
    total = 0
    for m in reversed(body):
        l = _msg_len(m)
        if total + l > tail_budget and tail:
            break
        tail.insert(0, m)
        total += l
    dropped = body[: len(body) - len(tail)]
    digest_words = " ".join(str(m.get("content", ""))[:40] for m in dropped)[
        :summary_chars
    ]
    digest = {"role": "system", "content": "Summary: %s" % digest_words}
    return head + [digest] + tail


class MemoryOptimizer:
    # Strategies registered lazily at instance level, not in class body.
    _STRATEGIES: Dict[
        str, Callable[[List[Dict[str, Any]], int], List[Dict[str, Any]]]
    ] = {
        "sliding": _sliding,
        "append-stable": _append_stable,
        "hierarchical": _hierarchical,
        "rolling-summary": _rolling_summary,
    }

    def __init__(self) -> None:
        self.profiles: Dict[str, Dict[str, Any]] = {}
        # Register preset profiles
        self._register_preset("chatgpt", "sliding", 128000)
        self._register_preset("openai", "sliding", 128000)
        self._register_preset("deepseek", "append-stable", 128000)
        self._register_preset("gemini", "hierarchical", 1000000)
        self._register_preset("qwen", "rolling-summary", 32000)
        self._default_profile = {"strategy": "append-stable", "window": 32000}

    def _register_preset(self, provider: str, strategy: str, window: int) -> None:
        self.profiles[provider] = {"strategy": strategy, "window": window}

    def register_profile(self, provider: str, strategy: str, window: int) -> None:
        """Register or override a memory profile for a provider."""
        self.profiles[provider] = {"strategy": strategy, "window": window}

    def _profile_for(self, provider: str) -> Dict[str, Any]:
        return self.profiles.get(provider, self._default_profile)

    @staticmethod
    def _total(messages: List[Dict[str, Any]]) -> int:
        def _len(c):
            if isinstance(c, str):
                return len(c)
            try:
                return len(json.dumps(c, ensure_ascii=False))
            except Exception:
                return len(str(c))

        return sum(_len(m.get("content", "")) for m in messages)

    def _enforce_budget(
        self, messages: List[Dict[str, Any]], budget: int
    ) -> List[Dict[str, Any]]:
        """Guarantee result fits budget. Keeps the head pair and the tail intact."""
        suffix = "...[truncated to fit window]"
        if not messages:
            return messages
        trimmed: List[Dict[str, Any]] = [dict(m) for m in messages]
        if self._total(trimmed) <= budget:
            return trimmed
        # Drop middle messages (largest first); head pair and tail stay.
        while len(trimmed) > 3 and self._total(trimmed) > budget:
            idx = max(range(2, len(trimmed) - 1), key=lambda i: _msg_len(trimmed[i]))
            trimmed.pop(idx)
        # Truncate the largest remaining message; the suffix must fit too.
        while self._total(trimmed) > budget and len(trimmed) > 1:
            idx = max(range(len(trimmed)), key=lambda i: _msg_len(trimmed[i]))
            content = trimmed[idx].get("content", "")
            if not isinstance(content, str) or _msg_len(trimmed[idx]) == 0:
                trimmed.pop(idx)
                continue
            others = self._total(trimmed) - _msg_len(trimmed[idx])
            avail = budget - others - len(suffix)
            if avail < 0:
                trimmed.pop(idx)
                continue
            trimmed[idx] = {**trimmed[idx], "content": content[:avail] + suffix}
            break
        if self._total(trimmed) > budget and len(trimmed) == 1:
            content = trimmed[0].get("content", "")
            if isinstance(content, str):
                avail = max(0, budget - len(suffix))
                trimmed[0] = {**trimmed[0], "content": content[:avail] + suffix}
        return trimmed

    def optimize(
        self, provider: str, messages: List[Dict[str, Any]], reserve: int = 512
    ) -> Dict[str, Any]:
        profile = self._profile_for(provider)
        budget = max(1024, profile["window"] - reserve)
        before = self._total(messages)
        if before <= budget:
            return {
                "messages": messages,
                "strategy": profile["strategy"],
                "tokens_before": before,
                "tokens_after": before,
                "compressed": False,
            }
        fn = self._STRATEGIES.get(profile["strategy"], _append_stable)
        raw = fn(messages, budget)
        after = self._enforce_budget(raw, budget)
        return {
            "messages": after,
            "strategy": profile["strategy"],
            "tokens_before": before,
            "tokens_after": self._total(after),
            "compressed": True,
        }


# --------------------------------------------------------------------------
# A2AProtocol - agent-to-agent messaging
# --------------------------------------------------------------------------


@dataclass
class A2AMessage:
    type: str
    sender: str
    payload: Dict[str, Any]
    recipient: Optional[str] = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    timestamp: float = field(default_factory=time.time)
    reply_to: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "sender": self.sender,
            "recipient": self.recipient,
            "payload": self.payload,
            "reply_to": self.reply_to,
            "timestamp": self.timestamp,
        }


class A2AProtocol:
    def __init__(self) -> None:
        self.agents: Dict[str, Callable[[A2AMessage], Any]] = {}
        self.history: List[A2AMessage] = []
        self.max_history = 500

    def register_agent(self, name: str, handler: Callable[[A2AMessage], Any]) -> None:
        self.agents[name] = handler

    def unregister_agent(self, name: str) -> None:
        self.agents.pop(name, None)

    def route(self, message: A2AMessage) -> List[str]:
        """Deliver to recipients; returns the agent names that received it."""
        self.history.append(message)
        if len(self.history) > self.max_history:
            self.history = self.history[-self.max_history :]
        if message.recipient:
            targets = [message.recipient]
        else:
            targets = [n for n in self.agents if n != message.sender]
        delivered = []
        for name in targets:
            handler = self.agents.get(name)
            if handler is None:
                continue
            try:
                handler(message)
                delivered.append(name)
            except Exception:
                # a failing agent never breaks routing for the others
                continue
        return delivered

    def send(self, type_, sender, payload, recipient=None, reply_to=None):
        return self.route(
            A2AMessage(
                type=type_,
                sender=sender,
                payload=payload,
                recipient=recipient,
                reply_to=reply_to,
            )
        )


# --------------------------------------------------------------------------
# CoordinationHub - facade
# --------------------------------------------------------------------------


class CoordinationHub:
    def __init__(self, state_path: Optional[str] = None) -> None:
        self.providers = ProviderManager()
        self.memory = MemoryOptimizer()
        self.a2a = A2AProtocol()
        self.shared = SharedMemory(state_path or SHARED_MEMORY_PATH)

    def prepare_request(
        self,
        provider,
        messages,
        allow_over_quota: bool = False,
        task: Optional[str] = None,
    ):
        """Pick the best provider, inject shared memory, trim for it."""
        chosen = provider
        pm = self.providers
        if (
            chosen not in pm.providers
            or not pm.is_healthy(chosen)
            or not pm.has_quota(chosen)
        ):
            chosen = pm.good_provider() or chosen
        task_key = task or self.shared.last_task or "default"
        merged = self.shared.prepare(task_key, messages)
        optimized = self.memory.optimize(chosen, merged)
        return {
            "provider": chosen,
            "requested": provider,
            "switched": chosen != provider,
            "task": task_key,
            "messages": optimized["messages"],
            "strategy": optimized["strategy"],
            "tokens_before": optimized["tokens_before"],
            "tokens_after": optimized["tokens_after"],
            "compressed": optimized["compressed"],
            "shared_memory": self.shared.stats(),
        }

    def stats(self) -> Dict[str, Any]:
        return {
            "providers": self.providers.stats(),
            "memory_profiles": {k: dict(v) for k, v in self.memory.profiles.items()},
            "shared_memory": self.shared.stats(),
            "a2a_agents": list(self.a2a.agents),
            "a2a_messages": len(self.a2a.history),
        }


# --------------------------------------------------------------------------
# CoordinationHub - fallback main
# --------------------------------------------------------------------------


def main() -> None:
    pass


if __name__ == "__main__":
    main()
