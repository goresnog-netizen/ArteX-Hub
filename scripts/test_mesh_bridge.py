"""Live WS test of the project mesh wired into the running bridge.

The bridge uses a broker gateway, so a task parks as a pending call. This test
answers each parked call the way the extension will, then verifies the second
task actually received the first task's findings in its system prompt.

Run against a running bridge:  python -X utf8 scripts/test_mesh_bridge.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
import urllib.request

import websockets

HOST = "127.0.0.1:17613"
ROOT = r"C:\MRP\proyek 2026\snapgent-connector"
TOKEN = ""

PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"PASS {name}" + (f" {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"FAIL {name}" + (f" {detail}" if detail else ""))


def load_token() -> str:
    cfg = json.loads(open(f"{ROOT}\\config.json", encoding="utf-8").read())
    return cfg["bridge"]["auth"]["secret"]


async def main() -> None:
    global TOKEN
    TOKEN = load_token()
    headers = {"Authorization": f"Bearer {TOKEN}"}

    async with websockets.connect(
        f"ws://{HOST}", additional_headers=headers, open_timeout=5
    ) as ws:
        first = json.loads(await ws.recv())
        check("mesh_connected", first.get("type") == "connected", first.get("type"))

        # a two-task chain: task a must hand its result to task b
        project_id = f"prj_{uuid.uuid4().hex[:8]}"
        await ws.send(
            json.dumps(
                {
                    "type": "mesh_create_project",
                    "id": "m1",
                    "title": project_id,
                    "goal": "prove cross-agent handoff",
                    "tasks": [
                        {
                            "id": "a",
                            "title": "design",
                            "prompt": "design the module",
                            "preferProvider": "chatgpt",
                            "notifyPeers": ["deepseek"],
                        },
                        {
                            "id": "b",
                            "title": "implement",
                            "prompt": "implement the module",
                            "preferProvider": "deepseek",
                            "dependsOn": ["a"],
                        },
                    ],
                }
            )
        )
        created = json.loads(await ws.recv())
        check("mesh_project_created", created.get("type") == "mesh_project_created")
        project = created.get("project", {})
        pid = project.get("id")
        check("project_id_present", bool(pid), str(pid))
        check(
            "project_tasks_2",
            project.get("progress", {}).get("tasks") == 2
            or len(project.get("tasks", [])) == 2,
            json.dumps(project.get("progress")),
        )

        await ws.send(
            json.dumps({"type": "mesh_run_project", "id": "m2", "projectId": pid})
        )
        started = json.loads(await ws.recv())
        check(
            "mesh_run_started",
            started.get("type") == "mesh_run_started",
            started.get("type"),
        )

        # answer every parked task; the second one must see the first result
        systems: list[str] = []
        answered = 0
        pushed_threads: list[str] = []
        for _ in range(14):
            await ws.send(json.dumps({"type": "mesh_parked", "id": "p1"}))
            parked = json.loads(await ws.recv())
            if parked.get("type") not in (
                "mesh_parked",
                "mesh_error",
                "mesh_task",  # unsolicited push from the parked watcher
            ):
                break  # run summary arrived while we were polling
            if parked.get("type") == "mesh_task":
                # The bridge pushes ONE at a time: a second distinct threadId
                # must not appear before the first has been answered, or the
                # extension would be asked to type into two tabs at once.
                pushed = parked.get("task", {}).get("threadId")
                if pushed:
                    pushed_threads.append(pushed)
                check(
                    "mesh_push_serialized",
                    len(pushed_threads) <= answered + 1,
                    f"pushed={len(pushed_threads)} answered={answered}",
                )
                if answered and pushed not in pushed_threads[:answered]:
                    # A genuinely NEW task arriving this early is fine only when
                    # something has been answered; otherwise it is a second
                    # concurrent dispatch.
                    check(
                        "mesh_push_no_overlap",
                        len(pushed_threads) - answered <= 1,
                        f"pushed={len(pushed_threads)} answered={answered}",
                    )
                continue
            items = parked.get("parked", [])
            if not items:
                await asyncio.sleep(0.3)
                continue
            call = items[0]
            systems.append(call.get("system", ""))
            await ws.send(
                json.dumps(
                    {
                        "type": "mesh_task_result",
                        "id": "p2",
                        "threadId": call["thread_id"],
                        "text": f"ANSWERED-BY-TEST:{call.get('provider')}",
                    }
                )
            )
            acked = json.loads(await ws.recv())
            check(
                "mesh_task_resolved", acked.get("resolved") is True, json.dumps(acked)
            )
            answered += 1

        check("mesh_answered_two", answered == 2, str(answered))
        # the parked poll keeps replying; drain until the run summary arrives
        finished = {}
        if parked.get("type") == "mesh_run_finished":
            finished = parked
        for _ in range(10):
            if finished.get("type") == "mesh_run_finished":
                break
            try:
                finished = json.loads(await asyncio.wait_for(ws.recv(), timeout=8.0))
            except asyncio.TimeoutError:
                break
            if finished.get("type") == "mesh_run_finished":
                break
        check(
            "mesh_run_finished",
            finished.get("type") == "mesh_run_finished",
            json.dumps(finished)[:200],
        )

        await ws.send(json.dumps({"type": "mesh_status", "id": "m3", "projectId": pid}))
        status = await recv_for_id(ws, "m3")
        prog = status.get("status", {}).get("progress", {})
        check("mesh_all_done", prog.get("done") == 2, json.dumps(prog))

        check(
            "handoff_reached_peer",
            any("ANSWERED-BY-TEST:chatgpt" in s for s in systems),
            str([s[:120] for s in systems]),
        )

        # agent-to-agent message lands in the recipient inbox
        await ws.send(
            json.dumps(
                {
                    "type": "mesh_send_agent",
                    "id": "m4",
                    "sender": "chatgpt",
                    "recipient": "deepseek",
                    "note": "use async io",
                }
            )
        )
        sent = await recv_for_id(ws, "m4")
        delivered = sent.get("sent", {}).get("delivered")
        check(
            "mesh_agent_message_delivered",
            delivered == ["provider:deepseek"],
            str(delivered),
        )

        # unknown project is refused, not crashed
        await ws.send(
            json.dumps({"type": "mesh_run_project", "id": "m5", "projectId": "nope"})
        )
        bad = await recv_for_id(ws, "m5")
        check("mesh_unknown_project_refused", "error" in bad, json.dumps(bad)[:120])

    print(f"\nOVERALL: {'PASS' if FAIL == 0 else 'FAIL'}")
    sys.exit(1 if FAIL else 0)


async def recv_for_id(ws, want_id: str, timeout: float = 15.0) -> dict:
    """Read frames until the ack carrying want_id arrives.

    The broker emits unsolicited mesh_parked frames while a run is alive, so a
    plain recv() would answer the wrong frame.
    """
    while True:
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
        if msg.get("id") == want_id:
            return msg


def main_sync():
    asyncio.run(main())


if __name__ == "__main__":
    main_sync()
