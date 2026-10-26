"""In-memory state of the simulated company (Acme). The single source of truth
that both the agent (through its tools) and the verifier (through read APIs) see."""
from __future__ import annotations

import copy
import threading
from datetime import date, timedelta

LOCK = threading.RLock()

# Interview slots: next week, Mon-Fri, 10:00-17:00 on the hour.
def _next_monday(today: date | None = None) -> date:
    today = today or date.today()
    return today + timedelta(days=(7 - today.weekday()) or 7)


def all_slots() -> list[str]:
    mon = _next_monday()
    slots = []
    for d in range(5):
        day = mon + timedelta(days=d)
        for h in range(10, 18):
            slots.append(f"{day.isoformat()}T{h:02d}:00")
    return slots


EMPTY = {
    "roles": [],
    "applicants": [],
    "calendar": {"busy": [], "events": []},
    "outbox": [],
    "tickets": [],
    "chaos": {},
    "idempotency": {},   # key -> stored response
    "write_log": [],     # every committed write, for audits
}

STATE: dict = copy.deepcopy(EMPTY)


def reset(seed: dict) -> None:
    with LOCK:
        STATE.clear()
        STATE.update(copy.deepcopy(EMPTY))
        for k, v in copy.deepcopy(seed).items():
            STATE[k] = v


def snapshot() -> dict:
    with LOCK:
        return copy.deepcopy(STATE)


def chaos(name: str, default=None):
    return STATE["chaos"].get(name, default)


def consume_chaos(name: str) -> bool:
    """One-shot chaos flags: true the first time they are checked, then cleared."""
    with LOCK:
        if STATE["chaos"].get(name):
            STATE["chaos"][name] = False
            return True
        return False


def applicant(aid: str) -> dict | None:
    return next((a for a in STATE["applicants"] if a["id"] == aid), None)


def ticket(tid: str) -> dict | None:
    return next((t for t in STATE["tickets"] if t["id"] == tid), None)
