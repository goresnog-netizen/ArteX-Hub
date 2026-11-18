"""Project mesh - the layer that lets the connected AI platforms cooperate.

coordination_hub.py answers "which provider should take this call, and how do I
fit its context window". This module answers the next question: "these providers
are agents - how do they work on one project together?"

Design:
  Task            one unit of work, owned by exactly one provider run
  Project         a DAG of tasks plus a goal; persisted as JSON so a bridge
                  restart does not lose an in-flight project
  Gateway         the transport that actually performs a call. Two exist:
                  ApiGateway (real HTTP to a provider endpoint) and
                  MeshGateway (a broker for platforms reached through the
                  browser extension, where the user is already logged in)
  ProjectMesh     the runner: picks a provider per task through the hub
                  (quota, health, auto-switch), compresses context, calls,
                  records usage, and announces everything on the A2A bus

Cross-agent cooperation is not a broadcast log. Every agent has an inbox. When a
task finishes, its findings are pushed to the peers named in the task, and the
next task that agent runs is executed with those peer findings in its system
prompt. That is the loop that makes one agent's result change another agent's
next answer.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from coordination_hub import (
    AllProvidersExhausted,
    A2AMessage,
    CoordinationHub,
    ProviderConfig,
)

# Task states
PENDING = "pending"  # waiting on dependencies
READY = "ready"  # dependencies satisfied, not started
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"


class GatewayError(RuntimeError):
    """A provider transport failed in a way that is the provider's fault."""


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


@dataclass
class Task:
    id: str
    project_id: str
    title: str = ""
    prompt: str = ""
    system: str = ""
    depends_on: List[str] = field(default_factory=list)
    prefer_provider: str = ""
    notify_peers: List[str] = field(default_factory=list)
    status: str = READY
    attempts: int = 0
    result: str = ""
    error: str = ""
    provider_used: str = ""
    thread_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: float = field(default_factory=time.time)
    finished_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Task":
        known = {k: raw[k] for k in cls.__dataclass_fields__ if k in raw}
        return cls(**known)


@dataclass
class Project:
    id: str
    title: str = ""
    goal: str = ""
    status: str = PENDING
    tasks: List[Task] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    finished_at: float = 0.0

    def task(self, task_id: str) -> Optional[Task]:
        for t in self.tasks:
            if t.id == task_id:
                return t
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "goal": self.goal,
            "status": self.status,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "tasks": [t.to_dict() for t in self.tasks],
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Project":
        return cls(
            id=raw["id"],
            title=raw.get("title", ""),
            goal=raw.get("goal", ""),
            status=raw.get("status", PENDING),
            created_at=raw.get("created_at", time.time()),
            finished_at=raw.get("finished_at", 0.0),
            tasks=[Task.from_dict(t) for t in raw.get("tasks", [])],
        )

    def progress(self) -> Dict[str, int]:
        out = {PENDING: 0, READY: 0, RUNNING: 0, DONE: 0, FAILED: 0, CANCELLED: 0}
        for t in self.tasks:
            out[t.status] = out.get(t.status, 0) + 1
        return out


class TaskStore:
    """JSON-file persistence. Atomic replace so a crash cannot truncate it."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> Dict[str, Project]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return {p["id"]: Project.from_dict(p) for p in raw.get("projects", [])}

    def save(self, projects: Dict[str, Project]) -> None:
        payload = {"projects": [p.to_dict() for p in projects.values()]}
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        tmp.replace(self.path)


# ---------------------------------------------------------------------------
# Gateways
# ---------------------------------------------------------------------------


class ScriptedGateway:
    """Deterministic transport for tests and for the extension broker.

    `responder(provider, system, messages, task_hint)` returns the reply text.
    When `responder` is None the gateway brokers instead: `request()` parks a
    pending future that an outside client fulfils with `resolve()`.
    """

    name = "scripted"

    def __init__(
        self,
        responder: Optional[Callable[[str, str, List[Dict[str, str]]], str]] = None,
        # A real chat turn is minutes, not seconds: type + stream one reply on a
        # web UI. This has to exceed what the worker (MESH_ANSWER_TIMEOUT) allows
        # or the task dies in the bridge while the tab is still answering.
        timeout: float = 900.0,
    ) -> None:
        self.responder = responder
        self.timeout = timeout
        self.pending: Dict[str, asyncio.Future] = {}
        self.parked: Dict[str, Dict[str, Any]] = {}
        self.calls: List[Dict[str, Any]] = []

    async def complete(
        self,
        provider: str,
        system: str,
        messages: List[Dict[str, str]],
        meta: Optional[Dict[str, Any]] = None,
    ) -> str:
        call = {
            "provider": provider,
            "system": system,
            "messages": messages,
            "meta": meta or {},
        }
        self.calls.append(call)
        if self.responder is not None:
            await asyncio.sleep(0)
            return self.responder(provider, system, messages)
        thread_id = (meta or {}).get("thread_id") or uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self.parked[thread_id] = {
            "thread_id": thread_id,
            "provider": provider,
            "system": system,
            "messages": messages,
            "meta": meta or {},
            "project_id": (meta or {}).get("project_id", ""),
            "created_at": time.time(),
        }
        self.pending[thread_id] = fut
        try:
            return await asyncio.wait_for(fut, timeout=self.timeout)
        except asyncio.TimeoutError as exc:
            self.pending.pop(thread_id, None)
            self.parked.pop(thread_id, None)
            raise GatewayError(
                f"No client answered task {thread_id} in {self.timeout}s"
            ) from exc
        finally:
            self.pending.pop(thread_id, None)
            self.parked.pop(thread_id, None)

    def pending_tasks(self, project_id: str = "") -> List[Dict[str, Any]]:
        """What an outside client must answer right now.

        Scoped by project when one is given: a parked task from a cancelled or
        finished run stays in the dict (the broker only drops it on timeout or
        on resolution), and a flat list would hand that orphan to another
        project's watcher, which then waits forever for an answer that will
        never come.
        """
        rows = [
            dict(v)
            for v in self.parked.values()
            if not project_id or v.get("project_id") == project_id
        ]
        return rows

    def resolve(self, thread_id: str, result: str) -> bool:
        fut = self.pending.get(thread_id)
        if fut is None or fut.done():
            return False
        fut.set_result(result)
        return True


class ApiGateway:
    """Real HTTP transport. Keys come from provider config or the environment.

    Env names: SNAPGENT_PROVIDER_<NAME>_API_KEY (upper, dashes to underscores).
    Nothing is ever written back into config.json, so a key stays out of git.
    """

    name = "api"

    def __init__(self, hub: CoordinationHub, session: Any = None) -> None:
        self.hub = hub
        self._session = session

    def _key(self, provider: str) -> str:
        cfg = self.hub.providers.providers.get(provider)
        if cfg and cfg.api_key:
            return cfg.api_key
        env = f"SNAPGENT_PROVIDER_{provider.upper().replace('-', '_')}_API_KEY"
        return os.environ.get(env, "")

    def _session_get(self) -> Any:
        if self._session is None:
            import aiohttp

            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    def _build(
        self, provider: str, system: str, messages: List[Dict[str, str]]
    ) -> Dict[str, Any]:
        cfg = self.hub.providers.providers[provider]
        model = (cfg.models or ["default"])[0]
        if cfg.auth == "header" or provider == "gemini":
            turns = []
            for m in messages:
                role = "user" if m.get("role") != "assistant" else "model"
                turns.append({"role": role, "parts": [{"text": m.get("content", "")}]})
            body = {"contents": turns}
            if system:
                body["systemInstruction"] = {"parts": [{"text": system}]}
            return {
                "url": f"{cfg.endpoint.rstrip('/')}/{model}:generateContent",
                "params": ({"key": self._key(provider)} if self._key(provider) else {}),
                "headers": cfg.headers,
                "body": body,
                "style": "gemini",
            }
        body = {
            "model": model,
            "messages": ([{"role": "system", "content": system}] if system else [])
            + list(messages),
        }
        headers = {"Content-Type": "application/json"}
        headers.update(cfg.headers)
        if self._key(provider):
            headers["Authorization"] = f"Bearer {self._key(provider)}"
        return {
            "url": f"{cfg.endpoint.rstrip('/')}/chat/completions",
            "params": {},
            "headers": headers,
            "body": body,
            "style": "openai",
        }

    @staticmethod
    def _extract(style: str, data: Dict[str, Any]) -> str:
        if style == "gemini":
            try:
                return data["candidates"][0]["content"]["parts"][0]["text"]
            except (KeyError, IndexError, TypeError) as exc:
                raise GatewayError(f"gemini: no candidate text ({exc})") from exc
        choices = data.get("choices") or []
        if not choices:
            raise GatewayError("openai-style response carried no choices")
        return (choices[0].get("message") or {}).get("content", "")

    async def complete(
        self,
        provider: str,
        system: str,
        messages: List[Dict[str, str]],
        meta: Optional[Dict[str, Any]] = None,
    ) -> str:
        cfg = self.hub.providers.providers[provider]
        spec = self._build(provider, system, messages)
        sess = self._session_get()
        last_error = ""
        for attempt in range(max(1, cfg.max_retries)):
            try:
                async with sess.post(
                    spec["url"],
                    params=spec["params"] or None,
                    headers=spec["headers"],
                    json=spec["body"],
                    timeout=aiohttp_timeout(),
                ) as resp:
                    text = await resp.text()
                    if resp.status >= 400:
                        last_error = f"HTTP {resp.status}: {text[:300]}"
                    else:
                        try:
                            data = json.loads(text)
                        except json.JSONDecodeError as exc:
                            last_error = f"non-JSON body: {text[:200]}"
                        else:
                            return self._extract(spec["style"], data)
            except GatewayError:
                raise
            except Exception as exc:  # noqa: BLE001 - transport, retried
                last_error = f"{type(exc).__name__}: {exc}"
            await asyncio.sleep(min(2**attempt, 8) * 0.1)
        raise GatewayError(
            f"{provider} failed after {cfg.max_retries} attempts: {last_error}"
        )


def aiohttp_timeout() -> Any:
    import aiohttp

    return aiohttp.ClientTimeout(total=60)


# ---------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------


class ProjectMesh:
    """Runs projects as a DAG across the providers, with A2A cooperation."""

    def __init__(
        self,
        hub: CoordinationHub,
        gateway: Any,
        store: Optional[TaskStore] = None,
        project_root: Optional[Path] = None,
    ) -> None:
        self.hub = hub
        self.gateway = gateway
        self.store = store or TaskStore(
            (project_root or Path(__file__).resolve().parent)
            / "state"
            / "projects.json"
        )
        self.projects: Dict[str, Project] = self.store.load()
        self.inboxes: Dict[str, List[Dict[str, Any]]] = {}
        self._runs: Dict[str, asyncio.Task] = {}
        self._register_agents()

    # -- agents ------------------------------------------------------------

    def _register_agents(self) -> None:
        """Register every provider plus the operator as an A2A addressable agent."""
        self.hub.a2a.register_agent("operator", self._operator_inbox)
        for name in self.hub.providers.list_providers():
            self.hub.a2a.register_agent(f"provider:{name}", self._make_inbox(name))

    def refresh_agents(self) -> None:
        """Call after providers are (re)registered, so new ones get an agent."""
        for name in self.hub.providers.list_providers():
            agent = f"provider:{name}"
            if agent not in self.hub.a2a.agents:
                self.hub.a2a.register_agent(agent, self._make_inbox(name))

    def register_agent_inbox(self, agent: str) -> None:
        """Give an external agent (the Zed IDE) its own A2A inbox.

        External agents live outside this process. They are addressed like any
        other agent and read their inbox through the bridge protocol.
        """
        name = str(agent or "").strip()
        if not name or name in self.hub.a2a.agents:
            return
        self.hub.a2a.register_agent(name, self._make_inbox(name))

    def resolve_peer(self, peer: str) -> str:
        """Map a peer name onto a registered A2A agent.

        Bare provider names become "provider:<name>". Names that are already
        registered (operator, bridge, zed, ...) are used as they are, so an
        external agent can be notified directly.
        """
        name = str(peer or "").strip()
        if not name:
            return name
        if name in self.hub.a2a.agents:
            return name
        return f"provider:{name}"

    def _make_inbox(self, provider: str) -> Callable[[A2AMessage], Any]:
        def handle(message: A2AMessage) -> Dict[str, Any]:
            self.inboxes.setdefault(provider, []).append(
                {
                    "from": message.sender,
                    "type": message.type,
                    "payload": message.payload,
                    "at": message.timestamp,
                }
            )
            return {"ok": True, "queued": len(self.inboxes[provider])}

        return handle

    def _operator_inbox(self, message: A2AMessage) -> Dict[str, Any]:
        self.inboxes.setdefault("operator", []).append(
            {
                "from": message.sender,
                "type": message.type,
                "payload": message.payload,
                "at": message.timestamp,
            }
        )
        return {"ok": True}

    def drain_inbox(self, agent: str) -> List[Dict[str, Any]]:
        return self.inboxes.pop(agent, [])

    def inbox_depth(self, agent: str) -> int:
        return len(self.inboxes.get(agent, []))

    # -- projects ----------------------------------------------------------

    def create_project(
        self, title: str, goal: str, tasks: List[Dict[str, Any]], project_id: str = ""
    ) -> Project:
        pid = project_id or f"prj_{uuid.uuid4().hex[:8]}"
        built: List[Task] = []
        for i, spec in enumerate(tasks or []):
            tid = str(spec.get("id") or f"{pid}-t{i + 1}")
            built.append(
                Task(
                    id=tid,
                    project_id=pid,
                    title=spec.get("title", tid),
                    prompt=spec.get("prompt", ""),
                    system=spec.get("system", ""),
                    depends_on=[str(d) for d in spec.get("dependsOn", [])],
                    prefer_provider=spec.get("preferProvider", ""),
                    notify_peers=[str(p) for p in spec.get("notifyPeers", [])],
                )
            )
        project = Project(id=pid, title=title, goal=goal, tasks=built)
        self.projects[pid] = project
        self._recompute(project)
        self.store.save(self.projects)
        return project

    def add_task(self, project_id: str, spec: Dict[str, Any]) -> Optional[Task]:
        project = self.projects.get(project_id)
        if project is None:
            return None
        tid = str(spec.get("id") or f"{project_id}-t{len(project.tasks) + 1}")
        if project.task(tid) is not None:
            return None
        task = Task(
            id=tid,
            project_id=project_id,
            title=spec.get("title", tid),
            prompt=spec.get("prompt", ""),
            system=spec.get("system", ""),
            depends_on=[str(d) for d in spec.get("dependsOn", [])],
            prefer_provider=spec.get("preferProvider", ""),
            notify_peers=[str(p) for p in spec.get("notifyPeers", [])],
        )
        project.tasks.append(task)
        self._recompute(project)
        self.store.save(self.projects)
        return task

    def cancel_project(self, project_id: str) -> bool:
        project = self.projects.get(project_id)
        if project is None:
            return False
        run = self._runs.pop(project_id, None)
        if run is not None and not run.done():
            run.cancel()
        for task in project.tasks:
            if task.status in (READY, PENDING, RUNNING):
                task.status = CANCELLED
        project.status = CANCELLED
        self.store.save(self.projects)
        return True

    # -- readiness ---------------------------------------------------------

    def _recompute(self, project: Project) -> List[str]:
        """Mark every task READY or PENDING; return the ids that are READY."""
        ready: List[str] = []
        for task in project.tasks:
            if task.status in (DONE, RUNNING, CANCELLED, FAILED):
                continue
            unmet = [
                d
                for d in task.depends_on
                if (project.task(d) or Task(id=d, project_id="")).status != DONE
            ]
            task.status = READY if not unmet else PENDING
            if not unmet:
                ready.append(task.id)
        return ready

    @staticmethod
    def _sanitize_payload(payload: Any) -> str:
        """Sanitize peer payload: strip control chars except \\n \\t, cap 600, neutralise delimiters."""
        text = json.dumps(payload, ensure_ascii=False)[:600]
        # keep \n and \t, drop every other control char (C0 plus DEL)
        text = "".join(
            ch
            for ch in text
            if ch == "\n" or ch == "\t" or (32 <= ord(ch) and ord(ch) != 127)
        )
        text = text.replace(
            "<<<PEER_FINDINGS_UNTRUSTED>>>", "___PEER_FINDINGS_UNTRUSTED___"
        )
        text = text.replace(
            "<<<END_PEER_FINDINGS_UNTRUSTED>>>", "___END_PEER_FINDINGS_UNTRUSTED___"
        )
        return text

    def _peer_notes(self, agent: str, task: Task, cap: int = 6) -> str:
        """Drain the agent's inbox into a prompt block.

        Only messages from other agents count. The orchestrator's own status
        traffic (task.started / task.finished sent by "project-mesh") is routed
        to "operator", so it never reaches a working agent's prompt.
        """
        notes = self.drain_inbox(agent)
        for peer in task.notify_peers:
            notes.extend(self.drain_inbox(peer))
        notes = [
            n for n in notes if not str(n.get("from", "")).startswith("project-mesh")
        ]
        notes = [n for n in notes if not str(n.get("type", "")).startswith("task.")]
        if not notes:
            return ""
        kept = notes[-cap:]
        lines = [
            f"- {n['from']} ({n['type']}): <<<PEER_FINDINGS_UNTRUSTED>>>{self._sanitize_payload(n['payload'])}<<<END_PEER_FINDINGS_UNTRUSTED>>>"
            for n in kept
        ]
        return (
            "Findings from your peer agents, use them and do not repeat their work:\n"
            + "\n".join(lines)
        )

    def _system_for(self, task: Task, provider: str, notes: str) -> str:
        parts = [p for p in (task.system, notes) if p]
        base = (
            "\n\n".join(parts)
            if parts
            else "You are one agent inside a multi-agent build."
        )
        return f"{base}\n\nYou are agent '{provider}'. Task id: {task.id}."

    # -- running -----------------------------------------------------------

    async def run_project(
        self,
        project_id: str,
        max_concurrent: int = 3,
        max_attempts: int = 2,
        allow_over_quota: bool = False,
    ) -> Dict[str, Any]:
        project = self.projects.get(project_id)
        if project is None:
            return {"ok": False, "error": "unknown project"}

        # Only the mesh itself registers a run, so an external caller that
        # already holds a task for this project is not mistaken for a conflict.
        if asyncio.current_task() is not None:
            self._runs[project_id] = asyncio.current_task()
        try:
            return await self._run_waves(
                project, max_concurrent, max_attempts, allow_over_quota
            )
        finally:
            if self._runs.get(project_id) is asyncio.current_task():
                self._runs.pop(project_id, None)

    async def _run_waves(
        self,
        project: Project,
        max_concurrent: int = 3,
        max_attempts: int = 2,
        allow_over_quota: bool = False,
    ) -> Dict[str, Any]:
        gate = asyncio.Semaphore(max(1, max_concurrent))
        self.refresh_agents()

        async def one(task: Task) -> None:
            async with gate:
                await self._execute(task, max_attempts, allow_over_quota)

        while True:
            ready_ids = self._recompute(project)
            runnable = [project.task(i) for i in ready_ids]
            runnable = [t for t in runnable if t.status == READY]
            if not runnable:
                break
            await asyncio.gather(*(one(t) for t in runnable), return_exceptions=False)
            self.store.save(self.projects)

        counts = project.progress()
        project.status = (
            DONE
            if counts[FAILED] == 0 and counts[DONE] == len(project.tasks)
            else FAILED
        )
        project.finished_at = time.time()
        self.store.save(self.projects)

        if project.status == DONE:
            self.hub.a2a.route(
                A2AMessage(
                    type="project.completed",
                    sender="project-mesh",
                    payload={"project_id": project.id, "title": project.title},
                    recipient="operator",
                )
            )
        return {
            "ok": True,
            "project": project.id,
            "status": project.status,
            "progress": counts,
        }

    async def _execute(
        self, task: Task, max_attempts: int, allow_over_quota: bool
    ) -> None:
        project = self.projects.get(task.project_id)
        task.status = RUNNING
        for attempt in range(1, max_attempts + 1):
            task.attempts = attempt
            provider = self._pick(task, allow_over_quota)
            task.provider_used = provider
            # Peer findings are collected first, before this task's own
            # notifications exist, so a start notice never leaks into the prompt.
            notes = self._peer_notes(provider, task)
            self.hub.a2a.route(
                A2AMessage(
                    type="task.started",
                    sender="project-mesh",
                    payload={
                        "task": task.id,
                        "title": task.title,
                        "provider": provider,
                        "attempt": attempt,
                    },
                    recipient="operator",
                )
            )
            messages = [{"role": "user", "content": task.prompt}]
            started = time.time()
            try:
                prepared = self.hub.prepare_request(
                    provider, messages, allow_over_quota, task=task.id
                )
                provider = prepared["provider"]
                task.provider_used = provider
                tokens = prepared["tokens_after"]
                self.hub.providers.begin_request(provider)
                text = await self.gateway.complete(
                    provider,
                    self._system_for(task, provider, notes),
                    prepared["messages"],
                    {
                        "thread_id": task.thread_id,
                        "task": task.id,
                        "project_id": project.id,
                    },
                )
                latency_ms = (time.time() - started) * 1000.0
                self.hub.providers.end_request(provider, True, tokens, latency_ms)
                task.result = text
                task.status = DONE
                task.finished_at = time.time()
            except AllProvidersExhausted as exc:
                self.hub.providers.end_request(
                    provider, False, 0, (time.time() - started) * 1000.0
                )
                task.status = FAILED
                task.error = f"no provider available: {exc}"
                break
            except Exception as exc:  # noqa: BLE001 - any transport or code fault
                self.hub.providers.end_request(
                    provider, False, 0, (time.time() - started) * 1000.0
                )
                task.status = FAILED
                task.error = f"{type(exc).__name__}: {exc}"
                if attempt >= max_attempts:
                    break
                await asyncio.sleep(min(2**attempt, 4) * 0.05)
                continue
            self.hub.a2a.route(
                A2AMessage(
                    type="task.finished",
                    sender=f"provider:{provider}",
                    payload={
                        "task": task.id,
                        "provider": provider,
                        "result": task.result[:2000],
                        "duration_ms": int((task.finished_at - started) * 1000),
                    },
                    recipient="operator",
                )
            )
            for peer in task.notify_peers:
                self.hub.a2a.route(
                    A2AMessage(
                        type="peer.result",
                        sender=f"provider:{provider}",
                        payload={
                            "from_task": task.id,
                            "provider": provider,
                            "result": task.result[:2000],
                        },
                        recipient=self.resolve_peer(peer),
                    )
                )
            return

        self.hub.a2a.route(
            A2AMessage(
                type="task.failed",
                sender="project-mesh",
                payload={
                    "task": task.id,
                    "provider": task.provider_used,
                    "error": task.error[:500],
                },
                recipient="operator",
            )
        )
        if project is not None:
            self.store.save(self.projects)

    def _pick(self, task: Task, allow_over_quota: bool) -> str:
        if (
            task.prefer_provider
            and task.prefer_provider in self.hub.providers.providers
        ):
            if self.hub.providers.is_healthy(task.prefer_provider) and (
                allow_over_quota or self.hub.providers.has_quota(task.prefer_provider)
            ):
                return task.prefer_provider
        try:
            return self.hub.providers.get_best_provider(
                est_tokens=len(task.prompt) // 4, allow_over_quota=allow_over_quota
            )
        except AllProvidersExhausted:
            if task.prefer_provider:
                return task.prefer_provider
            raise

    # -- queries -----------------------------------------------------------

    def project_status(self, project_id: str) -> Dict[str, Any]:
        project = self.projects.get(project_id)
        if project is None:
            return {"ok": False, "error": "unknown project"}
        return {
            "ok": True,
            "project": project.id,
            "title": project.title,
            "goal": project.goal,
            "status": project.status,
            "progress": project.progress(),
            "tasks": [
                {
                    "id": t.id,
                    "title": t.title,
                    "status": t.status,
                    "provider": t.provider_used,
                    "attempts": t.attempts,
                    "depends_on": t.depends_on,
                    "error": t.error,
                }
                for t in project.tasks
            ],
        }

    def list_projects(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": p.id,
                "title": p.title,
                "goal": p.goal,
                "status": p.status,
                "progress": p.progress(),
                "tasks": len(p.tasks),
                "created_at": p.created_at,
            }
            for p in sorted(self.projects.values(), key=lambda x: x.created_at)
        ]

    def project_result(self, project_id: str) -> Dict[str, Any]:
        project = self.projects.get(project_id)
        if project is None:
            return {"ok": False, "error": "unknown project"}
        return {
            "ok": True,
            "project": project.id,
            "status": project.status,
            "goal": project.goal,
            "deliverables": [
                {
                    "task": t.id,
                    "title": t.title,
                    "provider": t.provider_used,
                    "result": t.result,
                }
                for t in project.tasks
                if t.status == DONE
            ],
            "failures": [
                {"task": t.id, "provider": t.provider_used, "error": t.error}
                for t in project.tasks
                if t.status == FAILED
            ],
        }

    def stats(self) -> Dict[str, Any]:
        return {
            "projects": len(self.projects),
            "a2a_agents": list(self.hub.a2a.agents),
            "a2a_messages": len(self.hub.a2a.history),
            "inbox_depth": {k: len(v) for k, v in self.inboxes.items()},
            "broker_pending": len(getattr(self.gateway, "pending", {}) or {}),
        }

    # -- direct agent-to-agent --------------------------------------------

    def send_between_agents(
        self, sender: str, recipient: str, note: str, kind: str = "note"
    ) -> Dict[str, Any]:
        sender = (
            f"provider:{sender}" if sender in self.hub.providers.providers else sender
        )
        recipient = self.resolve_peer(recipient)
        message = A2AMessage(
            type=kind, sender=sender, payload={"note": note}, recipient=recipient
        )
        delivered = self.hub.a2a.route(message)
        return {
            "ok": bool(delivered),
            "delivered": delivered,
            "recipient": recipient,
            "message": message.to_dict(),
        }
