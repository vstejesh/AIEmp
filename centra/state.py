"""Durable run state. Saved after every step, so a run that pauses for a human
(or a process that restarts) resumes from exactly where it stopped."""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

RUNS_DIR = Path(__file__).resolve().parent.parent / "runs"


@dataclass
class Run:
    sop_id: str
    sop_version: int
    goal: str
    id: str = field(default_factory=lambda: time.strftime("%m%d-%H%M%S-") + uuid.uuid4().hex[:4])
    scenario: str | None = None
    status: str = "running"          # running | waiting_human | completed | failed | verification_failed
    created: float = field(default_factory=time.time)
    # What the employee knows and has done
    plan: list[dict] = field(default_factory=list)          # {milestone, done_when, status}
    facts: dict[str, dict] = field(default_factory=dict)    # subject -> field -> {value, evidence}
    decisions: dict[str, dict] = field(default_factory=dict)  # subject -> {outcome, why}
    done: dict[str, list[str]] = field(default_factory=dict)  # subject -> completed actions
    unknown_outcome: list[list[str]] = field(default_factory=list)  # [action, subject] awaiting reconcile
    approvals: dict[str, str] = field(default_factory=dict)  # action_key -> approved|rejected
    pending: dict | None = None                              # question or approval waiting on a human
    human_answers: list[dict] = field(default_factory=list)
    observations: list[str] = field(default_factory=list)   # raw texts the employee has seen (for evidence checks)
    sop_issues: list[str] = field(default_factory=list)
    security_flags: list[dict] = field(default_factory=list)  # instruction-like text found in data
    # System-of-record snapshot taken by code at start (never shown to the model)
    scope: dict[str, dict] = field(default_factory=dict)
    # Audit trail
    log: list[dict] = field(default_factory=list)
    steps: int = 0
    finish_attempts: int = 0
    last_url: str | None = None
    verification: dict | None = None
    report: dict | None = None
    usage: dict = field(default_factory=dict)
    error: str | None = None

    @property
    def dir(self) -> Path:
        d = RUNS_DIR / self.id
        (d / "shots").mkdir(parents=True, exist_ok=True)
        return d

    def save(self) -> None:
        tmp = self.dir / "run.json.tmp"
        tmp.write_text(json.dumps(asdict(self), indent=1, default=str))
        tmp.replace(self.dir / "run.json")

    @classmethod
    def load(cls, run_id: str) -> "Run":
        return cls(**json.loads((RUNS_DIR / run_id / "run.json").read_text()))

    def subject_facts(self, subject: str) -> dict:
        return {k: v["value"] for k, v in self.facts.get(subject, {}).items()}


def list_runs() -> list[dict]:
    out = []
    if RUNS_DIR.exists():
        for p in sorted(RUNS_DIR.glob("*/run.json"), reverse=True):
            d = json.loads(p.read_text())
            out.append({k: d[k] for k in ("id", "sop_id", "goal", "status", "scenario", "created")})
    return out
