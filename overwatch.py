#!/usr/bin/env python3
"""
overwatch.py — MyAi Agent Overwatch
====================================
Self-healing watchdog for the MyAi agent fleet.

Every CHECK_INTERVAL seconds it:
  1. Pulls the live agent list from the coordinator
  2. Probes each agent directly (HTTP + heartbeat staleness)
  3. Drives a per-agent state machine:
       HEALTHY → DEGRADED → DOWN → RECOVERING → HEALTHY
  4. On DOWN: fires a recovery action from agents.yaml (restart via
     infra-gateway, or alert-only for unreachable agents like Tanner)
  5. Sends a Slack message on every state transition

Recovery cooldowns and per-agent restart caps prevent restart storms.
"""

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import aiohttp
import yaml

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("overwatch")

# ── Config (env-driven) ───────────────────────────────────────────────────────
COORDINATOR_URL   = os.getenv("COORDINATOR_URL",    "http://10.42.42.142:8000")
GATEWAY_URL       = os.getenv("GATEWAY_URL",        "https://gateway.infinihash.com")
GATEWAY_TOKEN     = os.getenv("GATEWAY_TOKEN",      "")
SLACK_TOKEN       = os.getenv("SLACK_TOKEN",        "")   # xoxp- token on Brain
SLACK_CHANNEL     = os.getenv("SLACK_CHANNEL",      "#myai-ops")
MYAI_API_KEY      = os.getenv("MYAI_API_KEY",       "")

CHECK_INTERVAL    = int(os.getenv("CHECK_INTERVAL",    "30"))   # seconds
PROBE_TIMEOUT     = int(os.getenv("PROBE_TIMEOUT",     "8"))    # seconds per probe
FAILURE_THRESH    = int(os.getenv("FAILURE_THRESH",    "2"))    # failures → DOWN
RECOVERY_THRESH   = int(os.getenv("RECOVERY_THRESH",   "2"))    # successes → HEALTHY
RESTART_COOLDOWN  = int(os.getenv("RESTART_COOLDOWN",  "120"))  # secs between restarts
MAX_RESTARTS      = int(os.getenv("MAX_RESTARTS",      "3"))    # per window before escalation
RESTART_WINDOW    = int(os.getenv("RESTART_WINDOW",    "3600")) # window for MAX_RESTARTS
HEARTBEAT_STALE   = int(os.getenv("HEARTBEAT_STALE",   "90"))   # seconds before stale
AGENTS_CONFIG     = Path(os.getenv("AGENTS_CONFIG",    "/opt/myai-overwatch/agents.yaml"))
STATE_FILE        = Path(os.getenv("STATE_FILE",       "/var/lib/myai-overwatch/state.json"))

# ── State machine ─────────────────────────────────────────────────────────────
class AgentState(str, Enum):
    HEALTHY    = "healthy"
    DEGRADED   = "degraded"
    DOWN       = "down"
    RECOVERING = "recovering"
    UNKNOWN    = "unknown"

# Emoji labels for Slack
_STATE_EMOJI = {
    AgentState.HEALTHY:    "✅",
    AgentState.DEGRADED:   "⚠️",
    AgentState.DOWN:       "🔴",
    AgentState.RECOVERING: "🔄",
    AgentState.UNKNOWN:    "❓",
}

@dataclass
class AgentRecord:
    """Runtime health record for a single agent."""
    agent_id:        str
    name:            str
    host:            str
    port:            int
    state:           AgentState = AgentState.UNKNOWN
    consecutive_ok:  int = 0
    consecutive_fail: int = 0
    restart_times:   list = field(default_factory=list)  # epoch timestamps
    last_restart_at: float = 0.0
    last_notified:   AgentState = AgentState.UNKNOWN
    last_latency_ms: float = 0.0


# ── Config loader ─────────────────────────────────────────────────────────────
def load_agents_config() -> dict:
    """Load agents.yaml; return empty dict if missing."""
    if AGENTS_CONFIG.exists():
        with AGENTS_CONFIG.open() as f:
            return yaml.safe_load(f) or {}
    log.warning("agents.yaml not found at %s — using defaults (alert-only)", AGENTS_CONFIG)
    return {}

def match_recovery(name: str, config: dict) -> dict:
    """
    Find the best recovery entry for an agent by name.
    Checks exact match first, then substring, then 'default'.
    """
    name_lower = name.lower()
    # exact
    if name_lower in config:
        return config[name_lower]
    # substring scan
    for key, val in config.items():
        if key != "default" and key in name_lower:
            return val
    return config.get("default", {"method": "alert_only"})


# ── Gateway helpers ───────────────────────────────────────────────────────────
async def gateway_post(session: aiohttp.ClientSession, path: str, payload: dict) -> dict:
    """POST to infra-gateway; raises on non-2xx."""
    url = f"{GATEWAY_URL}{path}"
    headers = {
        "Authorization": f"Bearer {GATEWAY_TOKEN}",
        "Content-Type":  "application/json",
    }
    async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
        body = await resp.json(content_type=None)
        if resp.status >= 400:
            raise RuntimeError(f"gateway {path} → {resp.status}: {body}")
        return body

async def restart_agent(session: aiohttp.ClientSession, record: AgentRecord, recovery: dict) -> str:
    """
    Execute the appropriate recovery action.
    Returns a human-readable outcome string.
    """
    method = recovery.get("method", "alert_only")

    if method == "alert_only":
        return "⚠️ alert-only — no automated recovery path"

    if method == "systemctl":
        host    = recovery["host"]
        service = recovery["service"]
        action  = recovery.get("action", "restart")
        result  = await gateway_post(session, f"/systemctl/{host}/{service}/{action}", {})
        return f"systemctl {action} {service} on {host} → {result.get('status', result)}"

    if method == "exec":
        host = recovery["host"]
        cmd  = recovery["cmd"]
        result = await gateway_post(session, f"/exec/{host}", {"command": cmd})
        return f"exec `{cmd}` on {host} → {result.get('stdout', result)}"

    if method == "deploy":
        service = recovery["service"]
        result  = await gateway_post(session, f"/deploy/{service}", {})
        return f"deploy {service} → {result.get('status', result)}"

    return f"unknown method {method!r}"


# ── Slack ─────────────────────────────────────────────────────────────────────
async def slack_send(session: aiohttp.ClientSession, text: str) -> None:
    """Send a Slack message via the infra-gateway /slack/reply relay.
    thread_ts is omitted so the message posts to the channel top-level.
    """
    if not GATEWAY_TOKEN:
        log.debug("SLACK skipped (no GATEWAY_TOKEN): %s", text)
        return
    try:
        await gateway_post(session, "/slack/reply", {
            "channel": SLACK_CHANNEL,
            "text":    text,
        })
    except Exception as exc:
        log.warning("Slack notify failed: %s", exc)


def _fmt_transition(record: AgentRecord, new_state: AgentState, detail: str = "") -> str:
    old_e = _STATE_EMOJI[record.state]
    new_e = _STATE_EMOJI[new_state]
    msg   = f"{old_e}→{new_e} *{record.name}* ({record.host}): `{record.state}` → `{new_state}`"
    if detail:
        msg += f"\n> {detail}"
    return msg


# ── Coordinator client ────────────────────────────────────────────────────────
async def fetch_agents(session: aiohttp.ClientSession) -> list[dict]:
    """
    Pull the live agent list from the coordinator.
    Tolerates both /api/v1/agents (list) and /v1/agents shapes.
    """
    endpoints = [
        f"{COORDINATOR_URL}/api/v1/agents",
        f"{COORDINATOR_URL}/v1/agents",
    ]
    headers = {}
    if MYAI_API_KEY:
        headers["Authorization"] = f"Bearer {MYAI_API_KEY}"

    for url in endpoints:
        try:
            async with session.get(url, headers=headers,
                                   timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    # normalize: might be a list or {"agents": [...]}
                    if isinstance(data, list):
                        return data
                    if isinstance(data, dict):
                        for key in ("agents", "data", "items", "results"):
                            if key in data and isinstance(data[key], list):
                                return data[key]
                    return []
        except Exception as exc:
            log.debug("fetch_agents %s: %s", url, exc)

    log.warning("Could not fetch agents from coordinator at %s", COORDINATOR_URL)
    return []


# ── Direct health probe ───────────────────────────────────────────────────────
async def probe_agent(session: aiohttp.ClientSession, record: AgentRecord) -> tuple[bool, float]:
    """
    HTTP GET /health (or /api/health) on the agent.
    Returns (ok: bool, latency_ms: float).
    """
    urls = [
        f"http://{record.host}:{record.port}/health",
        f"http://{record.host}:{record.port}/api/health",
        f"http://{record.host}:{record.port}/",
    ]
    for url in urls:
        t0 = time.monotonic()
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=PROBE_TIMEOUT)) as resp:
                latency = (time.monotonic() - t0) * 1000
                if resp.status < 500:
                    return True, latency
        except Exception:
            pass
    return False, 0.0


def heartbeat_ok(agent_data: dict) -> bool:
    """True if the coordinator's last_heartbeat for this agent is fresh enough."""
    hb = agent_data.get("last_heartbeat") or agent_data.get("last_seen") or agent_data.get("updated_at")
    if not hb:
        return True  # no heartbeat field — skip staleness check
    try:
        hb_ts = float(hb)
    except (TypeError, ValueError):
        # ISO string
        import datetime
        try:
            dt = datetime.datetime.fromisoformat(str(hb).rstrip("Z"))
            hb_ts = dt.timestamp()
        except Exception:
            return True
    return (time.time() - hb_ts) < HEARTBEAT_STALE


# ── State machine transition ──────────────────────────────────────────────────
def next_state(current: AgentState, probe_ok: bool, hb_ok: bool) -> AgentState:
    """Pure state-transition logic (no side effects)."""
    healthy = probe_ok and hb_ok

    if current == AgentState.UNKNOWN:
        return AgentState.HEALTHY if healthy else AgentState.DEGRADED

    if current == AgentState.HEALTHY:
        return AgentState.HEALTHY if healthy else AgentState.DEGRADED

    if current == AgentState.DEGRADED:
        return AgentState.HEALTHY if healthy else AgentState.DEGRADED

    if current == AgentState.DOWN:
        return AgentState.RECOVERING if healthy else AgentState.DOWN

    if current == AgentState.RECOVERING:
        return AgentState.HEALTHY if healthy else AgentState.DOWN

    return AgentState.UNKNOWN


# ── Overwatch engine ──────────────────────────────────────────────────────────
class Overwatch:
    def __init__(self):
        self.records:  dict[str, AgentRecord] = {}
        self.config:   dict = {}
        self._load_state()

    # ── Persistence ──────────────────────────────────────────────────────────
    def _load_state(self):
        """Restore persisted restart history (so cooldowns survive restarts)."""
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            if STATE_FILE.exists():
                saved = json.loads(STATE_FILE.read_text())
                for aid, data in saved.items():
                    r = AgentRecord(
                        agent_id       = aid,
                        name           = data.get("name", aid),
                        host           = data.get("host", ""),
                        port           = data.get("port", 8000),
                        state          = AgentState(data.get("state", "unknown")),
                        restart_times  = data.get("restart_times", []),
                        last_restart_at= data.get("last_restart_at", 0.0),
                    )
                    self.records[aid] = r
                log.info("Loaded state for %d agents", len(self.records))
        except Exception as exc:
            log.warning("Could not load state: %s", exc)

    def _save_state(self):
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                aid: {
                    "name":           r.name,
                    "host":           r.host,
                    "port":           r.port,
                    "state":          r.state.value,
                    "restart_times":  r.restart_times,
                    "last_restart_at": r.last_restart_at,
                }
                for aid, r in self.records.items()
            }
            STATE_FILE.write_text(json.dumps(payload, indent=2))
        except Exception as exc:
            log.warning("Could not save state: %s", exc)

    # ── Restart gate ─────────────────────────────────────────────────────────
    def _can_restart(self, record: AgentRecord) -> tuple[bool, str]:
        now = time.time()
        # cooldown
        if now - record.last_restart_at < RESTART_COOLDOWN:
            wait = int(RESTART_COOLDOWN - (now - record.last_restart_at))
            return False, f"cooldown {wait}s remaining"
        # window cap
        window_start = now - RESTART_WINDOW
        recent = [t for t in record.restart_times if t > window_start]
        if len(recent) >= MAX_RESTARTS:
            return False, f"{len(recent)} restarts in last {RESTART_WINDOW//60}m — escalation needed"
        return True, "ok"

    def _record_restart(self, record: AgentRecord):
        now = time.time()
        record.restart_times.append(now)
        record.last_restart_at = now
        # prune old entries outside window
        window_start = now - RESTART_WINDOW
        record.restart_times = [t for t in record.restart_times if t > window_start]

    # ── Main check loop ───────────────────────────────────────────────────────
    async def run(self):
        self.config = load_agents_config()
        log.info("Overwatch starting — check interval %ds", CHECK_INTERVAL)

        connector = aiohttp.TCPConnector(ssl=False, limit=20)
        async with aiohttp.ClientSession(connector=connector) as session:
            while True:
                try:
                    await self._tick(session)
                except Exception as exc:
                    log.error("tick error: %s", exc, exc_info=True)
                await asyncio.sleep(CHECK_INTERVAL)

    async def _tick(self, session: aiohttp.ClientSession):
        agents_data = await fetch_agents(session)

        if not agents_data:
            log.warning("No agents returned from coordinator")
            return

        log.info("Checking %d agents", len(agents_data))

        tasks = [self._check_agent(session, a) for a in agents_data]
        await asyncio.gather(*tasks, return_exceptions=True)
        self._save_state()

    async def _check_agent(self, session: aiohttp.ClientSession, agent_data: dict):
        # ── Normalize coordinator response ────────────────────────────────
        agent_id = str(
            agent_data.get("id") or
            agent_data.get("agent_id") or
            agent_data.get("name") or
            "unknown"
        )
        name = str(
            agent_data.get("display_name") or
            agent_data.get("name") or
            agent_id
        )
        host = str(agent_data.get("host") or agent_data.get("address") or "")
        port = int(agent_data.get("port") or agent_data.get("api_port") or 8000)

        # Skip agents with no addressable host
        if not host or host in ("localhost", "127.0.0.1") and not agent_data.get("probe_url"):
            log.debug("Skipping %s — no external host", name)
            return

        # ── Get or create record ──────────────────────────────────────────
        if agent_id not in self.records:
            self.records[agent_id] = AgentRecord(
                agent_id=agent_id, name=name, host=host, port=port
            )
        record = self.records[agent_id]
        record.name = name
        record.host = host
        record.port = port

        # ── Probe ─────────────────────────────────────────────────────────
        probe_ok, latency = await probe_agent(session, record)
        hb_ok = heartbeat_ok(agent_data)
        record.last_latency_ms = latency

        # ── Update consecutive counters ───────────────────────────────────
        if probe_ok and hb_ok:
            record.consecutive_ok   += 1
            record.consecutive_fail  = 0
        else:
            record.consecutive_fail += 1
            record.consecutive_ok    = 0

        # ── Determine effective health ────────────────────────────────────
        # Only call something DOWN after FAILURE_THRESH consecutive failures
        # Only call recovery after RECOVERY_THRESH consecutive successes
        effective_ok = (
            record.consecutive_ok >= RECOVERY_THRESH
            if record.state in (AgentState.DOWN, AgentState.RECOVERING)
            else probe_ok and hb_ok
        )
        effective_fail = record.consecutive_fail >= FAILURE_THRESH

        old_state = record.state
        if effective_fail and record.state not in (AgentState.DOWN,):
            new_state = AgentState.DOWN
        elif effective_fail and record.state == AgentState.DOWN:
            new_state = AgentState.DOWN
        else:
            new_state = next_state(record.state, effective_ok, hb_ok)

        log.debug(
            "%s  probe=%s hb=%s consec_fail=%d consec_ok=%d  %s→%s  %dms",
            name, probe_ok, hb_ok,
            record.consecutive_fail, record.consecutive_ok,
            old_state, new_state,
            int(latency),
        )

        # ── Transitions & actions ─────────────────────────────────────────
        if new_state != old_state:
            record.state = new_state
            await self._on_transition(session, record, old_state, new_state, agent_data)
        else:
            record.state = new_state

    async def _on_transition(
        self,
        session:     aiohttp.ClientSession,
        record:      AgentRecord,
        old_state:   AgentState,
        new_state:   AgentState,
        agent_data:  dict,
    ):
        log.info(
            "STATE CHANGE  %s  %s → %s",
            record.name, old_state.value, new_state.value
        )

        recovery     = match_recovery(record.name, self.config)
        detail_parts = []

        # ── Self-heal on DOWN ─────────────────────────────────────────────
        if new_state == AgentState.DOWN:
            can, reason = self._can_restart(record)
            if can:
                log.info("Attempting recovery for %s via %s", record.name, recovery.get("method"))
                try:
                    outcome = await restart_agent(session, record, recovery)
                    self._record_restart(record)
                    detail_parts.append(f"Recovery: {outcome}")
                    log.info("Recovery result for %s: %s", record.name, outcome)
                    # Optimistically move to RECOVERING
                    record.state = AgentState.RECOVERING
                    new_state    = AgentState.RECOVERING
                except Exception as exc:
                    detail_parts.append(f"Recovery failed: {exc}")
                    log.error("Recovery error for %s: %s", record.name, exc)
            else:
                detail_parts.append(f"Recovery blocked: {reason}")
                log.warning("Recovery blocked for %s: %s", record.name, reason)
                # Check if we've hit MAX_RESTARTS → escalation
                window_start = time.time() - RESTART_WINDOW
                recent_count = len([t for t in record.restart_times if t > window_start])
                if recent_count >= MAX_RESTARTS:
                    detail_parts.append(
                        f"🚨 *Escalation*: {MAX_RESTARTS} restarts in {RESTART_WINDOW//60}m — manual intervention required"
                    )

        # ── Build Slack message ───────────────────────────────────────────
        detail = "\n".join(detail_parts)
        slack_msg = _fmt_transition(
            AgentRecord(  # snapshot with old state for the "from" side
                agent_id=record.agent_id,
                name=record.name,
                host=record.host,
                port=record.port,
                state=old_state,
            ),
            new_state,
            detail,
        )

        # Add latency context for DEGRADED
        if new_state == AgentState.DEGRADED and record.last_latency_ms > 0:
            slack_msg += f"\n> latency: {record.last_latency_ms:.0f}ms"

        await slack_send(session, slack_msg)
        record.last_notified = new_state


# ── Status HTTP server (lightweight — for health checks on Overwatch itself) ──
async def status_server(overwatch: Overwatch):
    """Tiny HTTP server on :9999 so you can curl overwatch's own health."""
    from aiohttp import web

    async def handle_health(req):
        summary = {
            aid: {"name": r.name, "state": r.state.value, "latency_ms": r.last_latency_ms}
            for aid, r in overwatch.records.items()
        }
        return web.json_response({"status": "ok", "agents": summary})

    app = web.Application()
    app.router.add_get("/health", handle_health)
    app.router.add_get("/",       handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 9999)
    await site.start()
    log.info("Status server listening on :9999")


# ── Entrypoint ────────────────────────────────────────────────────────────────
async def main():
    ow = Overwatch()
    await asyncio.gather(
        ow.run(),
        status_server(ow),
    )

if __name__ == "__main__":
    asyncio.run(main())
