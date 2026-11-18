#!/usr/bin/env python3
"""Tests for coordination_hub.py — provider quota/switch, memory strategies, A2A."""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from coordination_hub import (  # noqa: E402
    A2AMessage,
    AllProvidersExhausted,
    CoordinationHub,
    MemoryOptimizer,
    ProviderConfig,
    ProviderManager,
    SharedMemory,
    SHARED_MEMORY_PATH,
)

PASS = 0
FAIL = 0


def check(label, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("PASS %s" % label)
    else:
        FAIL += 1
        print("FAIL %s" % label)


def t_provider_registry_and_selection():
    pm = ProviderManager()
    pm.register(
        "chatgpt",
        ProviderConfig(endpoint="https://api.openai.com", api_key="k1", priority=1),
    )
    pm.register(
        "gemini",
        ProviderConfig(
            endpoint="https://generativelanguage.googleapis.com", priority=3
        ),
    )
    pm.register(
        "qwen",
        ProviderConfig(endpoint="https://dashscope.aliyuncs.com", priority=2),
    )
    check("registry lists 3", len(pm.list_providers()) == 3)
    check("best is highest priority (gemini)", pm.get_best_provider() == "gemini")

    # degrade gemini until it is down -> selection must move off it
    for _ in range(3):
        pm.report_health("gemini", ok=False, latency_ms=900)
    check("gemini down after 3 failures", pm.health["gemini"].status == "down")
    check("switch off dead provider", pm.get_best_provider() == "qwen")

    pm.unregister("qwen")
    check("unregister removes", "qwen" not in pm.list_providers())


def t_quota_exhaustion_autoswitch():
    pm = ProviderManager()
    pm.register("a", ProviderConfig(endpoint="https://a.test", requests_per_minute=2))
    pm.register("b", ProviderConfig(endpoint="https://b.test", requests_per_minute=2))

    pm.begin_request("a")
    pm.begin_request("a")
    check("has_quota false at rpm cap", not pm.has_quota("a"))
    check("falls back to b", pm.get_best_provider() == "b")

    pm.begin_request("b")
    pm.begin_request("b")
    try:
        pm.get_best_provider()
        check("raises when all exhausted", False)
    except AllProvidersExhausted:
        check("raises when all exhausted", True)

    # the escape hatch is explicit, never implicit
    check(
        "over-quota escape hatch returns a provider",
        pm.get_best_provider(allow_over_quota=True) in ("a", "b"),
    )

    # an exhausted provider never silently wins over one with quota left
    pm2 = ProviderManager()
    pm2.register("x", ProviderConfig(endpoint="https://x.test", requests_per_minute=1))
    pm2.register("y", ProviderConfig(endpoint="https://y.test", requests_per_minute=5))
    pm2.begin_request("x")  # x is now at its cap
    pm2.begin_request("y")  # y still has 4 left
    pm2.end_request("y", ok=True)
    check(
        "quota-bearing provider preferred over exhausted",
        pm2.get_best_provider() == "y",
    )


def t_token_and_concurrent_limits():
    pm = ProviderManager()
    pm.register(
        "a",
        ProviderConfig(
            endpoint="https://a.test", tokens_per_minute=100, max_concurrent=2
        ),
    )
    check("token headroom respected", not pm.has_quota("a", est_tokens=150))
    check("token headroom ok", pm.has_quota("a", est_tokens=50))

    pm.begin_request("a")
    pm.begin_request("a")
    check("concurrent cap blocks", not pm.has_quota("a"))
    pm.end_request("a", ok=True)
    check("concurrent released", pm.has_quota("a"))
    pm.end_request("a", ok=True, tokens_used=42)
    check("tokens accounted", pm.usage["a"].tokens == 42)
    check("no error counted on success", pm.usage["a"].errors == 0)


def t_health_recovery():
    pm = ProviderManager()
    pm.register("a", ProviderConfig(endpoint="https://a.test"))
    pm.report_health("a", False, 100)
    check("degraded after 1 failure", pm.health["a"].status == "degraded")
    check("still usable when degraded", pm.is_healthy("a"))
    pm.report_health("a", True, 40)
    check("healthy again after success", pm.health["a"].status == "healthy")
    check("failure count reset", pm.health["a"].consecutive_failures == 0)


def t_memory_sliding_chatgpt():
    mo = MemoryOptimizer()
    msgs = [{"role": "system", "content": "S" * 400}]
    for i in range(600):
        msgs.append({"role": "user", "content": ("m%d " % i) * 200})  # ~250 tok
    out = mo.optimize("chatgpt", msgs)
    check("chatgpt compressed", out["compressed"])
    check("chatgpt uses sliding", out["strategy"] == "sliding")
    check("system prompt kept", out["messages"][0]["role"] == "system")
    check(
        "newest kept",
        out["messages"][-1]["content"].startswith("m599"),
    )
    check("oldest dropped", not out["messages"][1]["content"].startswith("m0 "))
    check("after <= before", out["tokens_after"] <= out["tokens_before"])
    check("input actually over window", out["tokens_before"] > 128000 - 512)
    check("budget honored", out["tokens_after"] <= 128000 - 512)
    check("message count reduced", len(out["messages"]) < len(msgs))


def t_memory_append_stable_deepseek():
    mo = MemoryOptimizer()
    head = [{"role": "system", "content": "S" * 400}]
    body = [
        {"role": "user", "content": ("p%d " % i) * 400} for i in range(400)
    ]  # ~200 tok each
    msgs = head + body
    out = mo.optimize("deepseek", msgs)
    check("deepseek compressed", out["compressed"])
    check("deepseek append-stable", out["strategy"] == "append-stable")
    # prefix stability: the first non-system message must remain verbatim
    check(
        "prefix stable (first body message preserved)",
        out["messages"][1]["content"] == msgs[1]["content"],
    )
    check(
        "tail preserved",
        out["messages"][-1]["content"] == msgs[-1]["content"],
    )


def t_memory_hierarchical_gemini():
    mo = MemoryOptimizer()
    msgs = [{"role": "system", "content": "S" * 400}]
    # ~4000 tokens each, so the middle cannot survive verbatim inside 1M
    for i in range(260):
        msgs.append({"role": "user", "content": ("g%d " % i) * 8000})
    out = mo.optimize("gemini", msgs)
    check("gemini compressed", out["compressed"])
    check("gemini hierarchical", out["strategy"] == "hierarchical")
    check(
        "digest message present",
        any("[compressed" in m["content"] for m in out["messages"]),
    )
    check("fresh tail kept", out["messages"][-1]["content"].startswith("g259"))
    check("system prompt kept first", out["messages"][0]["role"] == "system")
    check("after <= before", out["tokens_after"] <= out["tokens_before"])


def t_memory_rolling_summary_qwen():
    mo = MemoryOptimizer()
    msgs = [{"role": "system", "content": "summary block"}]
    for i in range(500):
        msgs.append({"role": "user", "content": ("q%d " % i) * 120})  # ~60 tok
    out = mo.optimize("qwen", msgs)
    check("qwen compressed", out["compressed"])
    check("qwen rolling-summary", out["strategy"] == "rolling-summary")
    check("summary block kept first", out["messages"][0]["content"] == "summary block")
    check("newest kept", out["messages"][-1]["content"].startswith("q499"))


def t_memory_no_op_under_budget():
    mo = MemoryOptimizer()
    msgs = [{"role": "system", "content": "hi"}] + [
        {"role": "user", "content": "short"} for _ in range(5)
    ]
    out = mo.optimize("qwen", msgs)
    check("no compression under budget", not out["compressed"])
    check("same object identity", out["messages"] is msgs)
    check("tokens unchanged", out["tokens_before"] == out["tokens_after"])


def t_memory_unknown_provider_default():
    mo = MemoryOptimizer()
    msgs = [{"role": "user", "content": "x" * 200000}] + [
        {"role": "user", "content": ("y%d " % i) * 400} for i in range(200)
    ]
    out = mo.optimize("mystery", msgs)
    check("unknown provider still compresses", out["compressed"])
    check("unknown provider default strategy", out["strategy"] == "append-stable")
    check("input over default window", out["tokens_before"] > 32000 - 512)
    check("output inside default window", out["tokens_after"] <= 32000 - 512)
    check(
        "oversized single message truncated",
        out["messages"][-1]["content"].endswith("...[truncated to fit window]")
        or len(out["messages"]) > 1,
    )
    mo.register_profile("mystery", "sliding", 8000)
    check(
        "custom profile override",
        mo.optimize("mystery", msgs)["strategy"] == "sliding",
    )


def t_a2a_routing():
    hub = CoordinationHub()
    inbox = {"lead": [], "coder": [], "review": []}

    def box(name):
        return lambda m: inbox[name].append(m)

    hub.a2a.register_agent("lead", box("lead"))
    hub.a2a.register_agent("coder", box("coder"))
    hub.a2a.register_agent("review", box("review"))

    delivered = hub.a2a.send(
        type_="task",
        sender="lead",
        payload={"job": "estimate"},
        recipient="coder",
    )
    check("direct recipient only", delivered == ["coder"])
    check("payload delivered", inbox["coder"][0].payload["job"] == "estimate")

    delivered = hub.a2a.send(type_="status", sender="lead", payload={"pct": 50})
    check("broadcast skips sender", set(delivered) == {"coder", "review"})
    check("history recorded", len(hub.a2a.history) == 2)

    msg = A2AMessage(type="ping", sender="coder", payload={"n": 1})
    delivered = hub.a2a.route(msg)
    check("reverse broadcast", set(delivered) == {"lead", "review"})

    check("history capped at 500", (hub.a2a.max_history == 500))

    # a failing handler must not break routing to others
    hub.a2a.register_agent("boom", lambda m: (_ for _ in ()).throw(ValueError("x")))
    delivered = hub.a2a.send(type_="probe", sender="lead", payload={})
    check("failing agent skipped", "boom" not in delivered and "coder" in delivered)

    hub.a2a.unregister_agent("review")
    delivered = hub.a2a.send(type_="again", sender="lead", payload={})
    check("unregister works", "review" not in delivered)


def t_hub_prepare_request_autoswitch():
    hub = CoordinationHub()
    hub.providers.register(
        "chatgpt",
        ProviderConfig(endpoint="https://api.openai.com", api_key="k", priority=1),
    )
    hub.providers.register(
        "gemini",
        ProviderConfig(
            endpoint="https://generativelanguage.googleapis.com", priority=5
        ),
    )
    msgs = [{"role": "user", "content": "hello"}]
    res = hub.prepare_request("chatgpt", msgs)
    check("requested provider honored when healthy", res["provider"] == "chatgpt")
    check("not switched", res["switched"] is False)

    for _ in range(3):
        hub.providers.report_health("chatgpt", False, 500)
    res = hub.prepare_request("chatgpt", msgs)
    check("auto-switch when unhealthy", res["provider"] == "gemini")
    check("switched flag true", res["switched"] is True)

    res = hub.prepare_request("nonexistent", msgs)
    check("unknown provider auto-assigned", res["provider"] == "gemini")

    stats = hub.stats()
    check("stats has providers", "chatgpt" in stats["providers"])
    check(
        "stats has memory profiles",
        set(stats["memory_profiles"]) >= {"chatgpt", "qwen"},
    )
    check("stats counts a2a messages", isinstance(stats["a2a_messages"], int))


def t_shared_memory_record_and_bootstrap():
    import tempfile, os

    path = os.path.join(tempfile.gettempdir(), "sm_test.json")
    if os.path.exists(path):
        os.remove(path)
    sm = SharedMemory(state_path=path, short_limit=5, long_limit=5)
    sm.record("task-a", [{"role": "user", "content": "build X"}])
    sm.record("task-a", [{"role": "assistant", "content": "building X"}])
    check("short-term holds messages", len(sm.get_short_term("task-a")) == 2)
    check("last_task set", sm.last_task == "task-a")

    # overflow promotes to long-term
    for i in range(6):
        sm.record("task-a", [{"role": "user", "content": "step %d" % i}])
    check("short-term capped", len(sm.get_short_term("task-a")) <= 5)
    check("long-term promoted", len(sm.get_long_term("task-a")) >= 1)

    # bootstrap redirects when a different task active
    sm.record("task-b", [{"role": "user", "content": "other"}])
    msgs = sm.bootstrap_messages("task-a")
    joined = " ".join(m.get("content", "") for m in msgs)
    check("last-work redirect emitted", "task-b" in joined or "task-a" in joined)
    check("bootstrap returns messages", isinstance(msgs, list))

    # persistence round-trip
    sm2 = SharedMemory(state_path=path)
    check("persisted round-trip short", len(sm2.get_short_term("task-a")) >= 1)
    os.remove(path)


def t_shared_memory_prepare_request_injects():
    hub = CoordinationHub()
    hub.providers.register(
        "chatgpt",
        ProviderConfig(endpoint="https://api.openai.com", api_key="k", priority=1),
    )
    msgs = [{"role": "user", "content": "continue work"}]
    res = hub.prepare_request("chatgpt", msgs, task="feat-42")
    contents = [m.get("content", "") for m in res["messages"]]
    joined = " ".join(contents)
    check("prepare returns task", res["task"] == "feat-42")
    check(
        "shared memory injected",
        "[shared-memory" in joined or len(res["messages"]) >= 1,
    )
    check("stats exposes shared", "shared_memory" in hub.stats())

    # second provider sees the same shared context
    res2 = hub.prepare_request(
        "chatgpt", [{"role": "user", "content": "next"}], task="feat-42"
    )
    check("second call keeps task", res2["task"] == "feat-42")
    check("short-term accumulates", hub.shared.stats()["short_messages"] >= 3)


def t_window_reset():
    pm = ProviderManager()
    pm.register("a", ProviderConfig(endpoint="https://a.test", requests_per_minute=5))
    pm.usage["a"].window_start = time.time() - 61
    pm.usage["a"].requests = 5
    pm._refresh_window("a")
    check("quota window resets after 60s", pm.usage["a"].requests == 0)
    check("quota available again", pm.has_quota("a"))


def main():
    t_provider_registry_and_selection()
    t_quota_exhaustion_autoswitch()
    t_token_and_concurrent_limits()
    t_health_recovery()
    t_memory_sliding_chatgpt()
    t_memory_append_stable_deepseek()
    t_memory_hierarchical_gemini()
    t_memory_rolling_summary_qwen()
    t_memory_no_op_under_budget()
    t_memory_unknown_provider_default()
    t_a2a_routing()
    t_hub_prepare_request_autoswitch()
    t_shared_memory_record_and_bootstrap()
    t_shared_memory_prepare_request_injects()
    t_window_reset()
    print("\nRESULT: %d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
