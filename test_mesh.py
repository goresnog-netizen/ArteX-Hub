"""Tests for the project mesh (task DAG + cross-agent A2A cooperation).

Every test runs against ScriptedGateway, so nothing touches the network. The API
transport is code-complete but its live path is only exercised when a provider
key is present; see docs/USAGE.md.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

from coordination_hub import CoordinationHub, ProviderConfig
from mesh import (
    DONE,
    FAILED,
    PENDING,
    READY,
    ApiGateway,
    ProjectMesh,
    ScriptedGateway,
    TaskStore,
)

PASS = 0
FAIL = 0
NOTES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"PASS {name}" + (f" {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"FAIL {name}" + (f" {detail}" if detail else ""))


def hub_with_providers(**overrides) -> CoordinationHub:
    hub = CoordinationHub()
    specs = {
        "chatgpt": (1, 60, 100000),
        "deepseek": (2, 60, 100000),
        "gemini": (3, 60, 100000),
        "qwen": (4, 60, 100000),
    }
    for name, (priority, rpm, tpm) in specs.items():
        cfg = ProviderConfig(
            endpoint="https://example.invalid/v1",
            api_key="",
            models=["m-1"],
            priority=priority,
            requests_per_minute=overrides.get(f"{name}_rpm", rpm),
            tokens_per_minute=tpm,
            max_concurrent=overrides.get(f"{name}_conc", 3),
            timeout=5.0,
            max_retries=2,
        )
        hub.providers.register(name, cfg)
        hub.memory.register_profile(name, "sliding", 4000)
    hub.memory.register_profile("chatgpt", "sliding", 4000)
    hub.memory.register_profile("deepseek", "append-stable", 4000)
    hub.memory.register_profile("gemini", "hierarchical", 4000)
    hub.memory.register_profile("qwen", "rolling-summary", 4000)
    return hub


def new_mesh(tmp: Path, responder=None, **overrides) -> ProjectMesh:
    store = TaskStore(tmp / "projects.json")
    return ProjectMesh(
        hub_with_providers(**overrides), ScriptedGateway(responder), store
    )


def run_dag(tmp: Path, tasks: list[dict], responder) -> tuple[ProjectMesh, str]:
    mesh = new_mesh(tmp, responder)
    project = mesh.create_project("p", "goal", tasks)
    asyncio.run(mesh.run_project(project.id))
    return mesh, project.id


def main() -> None:
    # 1. dependency order + all tasks finish
    with tempfile.TemporaryDirectory() as d:
        order: list[str] = []

        def responder(provider, system, messages):
            # Shared-memory context is injected before the live prompt and is
            # replayed as role="system", so the live request is the LAST
            # role="user" message.
            user_msgs = [m for m in messages if m.get("role") == "user"]
            prompt = user_msgs[-1]["content"] if user_msgs else messages[0]["content"]
            order.append(prompt)
            return f"[{provider}] done:{prompt}"

        mesh, pid = run_dag(
            Path(d),
            [
                {"id": "a", "title": "design", "prompt": "step-a"},
                {"id": "b", "title": "impl", "prompt": "step-b", "dependsOn": ["a"]},
                {"id": "c", "title": "test", "prompt": "step-c", "dependsOn": ["b"]},
            ],
            responder,
        )
        st = mesh.project_status(pid)
        check("dag_all_done", st["progress"][DONE] == 3, json.dumps(st["progress"]))
        check("dag_order", order == ["step-a", "step-b", "step-c"], str(order))

    # 2. quota auto-switch: chatgpt gets 1 req/min, later tasks must move off it
    with tempfile.TemporaryDirectory() as d:
        store = TaskStore(Path(d) / "projects.json")
        hub = hub_with_providers(chatgpt_rpm=1)
        gw = ScriptedGateway(lambda p, s, m: f"[{p}] ok")
        mesh = ProjectMesh(hub, gw, store)
        project = mesh.create_project(
            "quota",
            "goal",
            [
                {"id": "a", "preferProvider": "chatgpt", "prompt": "one"},
                {"id": "b", "preferProvider": "chatgpt", "prompt": "two"},
                {"id": "c", "preferProvider": "chatgpt", "prompt": "three"},
            ],
        )
        asyncio.run(mesh.run_project(project.id))
        used = [t.provider_used for t in project.tasks]
        check("quota_first_stays", used[0] == "chatgpt", used[0])
        check(
            "quota_switches", used[1] != "chatgpt" and used[2] != "chatgpt", str(used)
        )
        stats = hub.providers.stats()
        check(
            "quota_usage_tracked",
            stats["chatgpt"]["usage"]["requests"] == 1,
            json.dumps(stats["chatgpt"]["usage"]),
        )

    # 3. peer findings reach the next agent's system prompt
    with tempfile.TemporaryDirectory() as d:
        seen: dict[str, str] = {}

        def responder(provider, system, messages):
            seen[provider] = system
            return f"RESULT-FROM-{provider}-on-{messages[0]['content']}"

        mesh, pid = run_dag(
            Path(d),
            [
                {
                    "id": "a",
                    "preferProvider": "chatgpt",
                    "prompt": "first half",
                    "notifyPeers": ["deepseek"],
                },
                {
                    "id": "b",
                    "preferProvider": "deepseek",
                    "prompt": "second half",
                    "dependsOn": ["a"],
                },
            ],
            responder,
        )
        check(
            "peer_notes_injected",
            "RESULT-FROM-chatgpt" in seen["deepseek"],
            seen["deepseek"][:160],
        )
        check(
            "peer_notes_absent_first", "Findings from your peer" not in seen["chatgpt"]
        )

    # 4. retry then success
    with tempfile.TemporaryDirectory() as d:
        attempts = {"n": 0}

        def flaky(provider, system, messages):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("temporary transport fault")
            return "recovered"

        mesh, pid = run_dag(Path(d), [{"id": "a", "prompt": "x"}], flaky)
        st = mesh.project_status(pid)
        check("retry_succeeds", st["progress"][DONE] == 1, json.dumps(st["progress"]))
        check("retry_counted", attempts["n"] == 2, str(attempts["n"]))

    # 5. hard failure is recorded, project reports FAILED
    with tempfile.TemporaryDirectory() as d:

        def always_bad(provider, system, messages):
            raise RuntimeError("provider down")

        mesh, pid = run_dag(
            Path(d),
            [
                {"id": "a", "prompt": "x"},
                {"id": "b", "prompt": "y", "dependsOn": ["a"]},
            ],
            always_bad,
        )
        st = mesh.project_status(pid)
        check(
            "hard_fail_recorded",
            st["progress"][FAILED] == 1,
            json.dumps(st["progress"]),
        )
        check(
            "hard_fail_dependents_pending",
            st["progress"][PENDING] == 1,
            json.dumps(st["progress"]),
        )
        res = mesh.project_result(pid)
        check(
            "failure_carries_error",
            "provider down" in res["failures"][0]["error"],
            res["failures"][0]["error"][:80],
        )

    # 6. persistence: a fresh mesh instance sees the finished project
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        mesh, pid = run_dag(
            root, [{"id": "a", "prompt": "persist me"}], lambda p, s, m: "stored"
        )
        reopened = ProjectMesh(
            hub_with_providers(),
            ScriptedGateway(lambda p, s, m: ""),
            TaskStore(root / "projects.json"),
        )
        res = reopened.project_result(pid)
        check(
            "persistence_ok",
            res["ok"] and res["deliverables"][0]["result"] == "stored",
            json.dumps(res)[:120],
        )

    # 7. agent-to-agent message lands in the recipient inbox
    with tempfile.TemporaryDirectory() as d:
        mesh, pid = run_dag(
            Path(d),
            [{"id": "a", "preferProvider": "chatgpt", "prompt": "x"}],
            lambda p, s, m: "ok",
        )
        out = mesh.send_between_agents("chatgpt", "deepseek", "use async io")
        check(
            "a2a_delivered",
            out["ok"] and out["delivered"] == ["provider:deepseek"],
            json.dumps(out["delivered"]),
        )
        check(
            "a2a_inbox_depth",
            mesh.inbox_depth("deepseek") == 1,
            str(mesh.inbox_depth("deepseek")),
        )
        follow = mesh.create_project(
            "follow",
            "goal",
            [{"id": "f1", "preferProvider": "deepseek", "prompt": "now do it"}],
        )
        asyncio.run(mesh.run_project(follow.id))
        sys_prompt = mesh.gateway.calls[-1]["system"]
        check("a2a_note_reaches_prompt", "use async io" in sys_prompt, sys_prompt[:200])

    # 8. broker mode: no responder, an outside client fulfils the call

    async def run_broker(d):
        store = TaskStore(Path(d) / "projects.json")
        hub = hub_with_providers()
        gw = ScriptedGateway(responder=None, timeout=5.0)
        mesh = ProjectMesh(hub, gw, store)
        project = mesh.create_project(
            "broker", "goal", [{"id": "a", "prompt": "answer me"}]
        )
        runner = asyncio.create_task(mesh.run_project(project.id))
        for _ in range(200):
            await asyncio.sleep(0)
            if gw.pending:
                break
        pending = list(gw.pending.keys())
        check("broker_parks_call", len(pending) == 1, str(pending))
        if pending:
            gw.resolve(pending[0], "human answered")
        await runner
        check(
            "broker_result_stored",
            project.tasks[0].result == "human answered",
            str(project.tasks[0].result),
        )
        check("broker_no_leftover", len(gw.pending) == 0)

    with tempfile.TemporaryDirectory() as d:
        asyncio.run(run_broker(d))

    # 9. ApiGateway shape without keys: URL/headers/auth are correct, no key leak
    hub = hub_with_providers()
    api = ApiGateway(hub)
    spec = api._build("chatgpt", "be brief", [{"role": "user", "content": "hi"}])
    check(
        "api_openai_url",
        spec["url"] == "https://example.invalid/v1/chat/completions",
        spec["url"],
    )
    check(
        "api_openai_body",
        spec["body"]["messages"][0]["role"] == "system",
        json.dumps(spec["body"])[:120],
    )
    check(
        "api_openai_auth",
        "Authorization" not in spec["headers"],
        json.dumps(spec["headers"]),
    )
    gspec = api._build("gemini", "be brief", [{"role": "user", "content": "hi"}])
    check("api_gemini_url", gspec["url"].endswith("m-1:generateContent"), gspec["url"])
    check("api_gemini_style", gspec["style"] == "gemini")
    check("api_gemini_contents", gspec["body"]["contents"][0]["role"] == "user")

    # 10. cancel stops a running project

    async def run_cancel(d):
        store = TaskStore(Path(d) / "projects.json")
        gw = ScriptedGateway(responder=None, timeout=30.0)
        mesh = ProjectMesh(hub_with_providers(), gw, store)
        project = mesh.create_project(
            "cancel",
            "goal",
            [
                {"id": "a", "prompt": "wait"},
                {"id": "b", "prompt": "wait2", "dependsOn": ["a"]},
            ],
        )
        runner = asyncio.create_task(mesh.run_project(project.id))
        for _ in range(200):
            await asyncio.sleep(0)
            if gw.pending:
                break
        mesh.cancel_project(project.id)
        try:
            await asyncio.wait_for(runner, timeout=2.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            runner.cancel()
        check(
            "cancel_marks_task",
            project.tasks[0].status in ("running", "cancelled"),
            project.tasks[0].status,
        )

    with tempfile.TemporaryDirectory() as d:
        asyncio.run(run_cancel(d))

    print(f"\nRESULT: {PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
