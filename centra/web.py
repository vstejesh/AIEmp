"""Operator console + API. One process hosts the console and the sandbox company;
the AI employee reaches the sandbox over HTTP and a real browser like any client."""
from __future__ import annotations

import os
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from sandbox import scenarios, world
from sandbox.app import router as sandbox_router

from . import manager, sop as sop_mod
from .runtime import Employee
from .state import RUNS_DIR, Run, list_runs

app = FastAPI(title="Centra")
app.include_router(sandbox_router, prefix="/sandbox")
BASE_URL = os.environ.get("SELF_URL", f"http://127.0.0.1:{os.environ.get('PORT', '8000')}")
STATIC = Path(__file__).parent / "static"

_active: dict[str, threading.Thread] = {}
_lock = threading.Lock()


def _busy() -> str | None:
    for rid, t in list(_active.items()):
        if t.is_alive():
            return rid
        _active.pop(rid, None)
    return None


def _spawn(emp: Employee):
    t = threading.Thread(target=emp.loop, daemon=True)
    _active[emp.run.id] = t
    t.start()


@app.on_event("startup")
def _startup():
    world.reset(scenarios.get("clean")["seed"])
    for r in list_runs():  # runs cut off by a restart become resumable instead of stuck
        if r["status"] == "running":
            run = Run.load(r["id"])
            run.status, run.error = "failed", "model unavailable: interrupted by server restart"
            run.save()


@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC / "index.html").read_text()


# ---------------------------------------------------------------- SOPs
class LearnReq(BaseModel):
    description: str
    owner: str
    id: str


@app.get("/api/sops")
def sops():
    return [s.model_dump() for s in sop_mod.list_all()]


@app.post("/api/sops/learn")
def learn(r: LearnReq):
    try:
        return manager.learn_sop(r.description, r.owner, r.id)
    except Exception as e:
        raise HTTPException(422, str(e))


class ApproveSop(BaseModel):
    sop: dict


@app.post("/api/sops/approve")
def approve_sop(r: ApproveSop):
    try:
        return manager.approve_sop(r.sop).model_dump()
    except Exception as e:
        raise HTTPException(422, str(e))


# ---------------------------------------------------------------- runs
class StartReq(BaseModel):
    sop_id: str
    goal: str
    scenario: str | None = None


@app.get("/api/scenarios")
def list_scenarios():
    return scenarios.ALL


@app.post("/api/runs")
def start(r: StartReq):
    with _lock:
        if (b := _busy()):
            raise HTTPException(409, f"run {b} is still working; the sandbox runs one employee at a time")
        if r.scenario:
            world.reset(scenarios.get(r.scenario)["seed"])
        try:
            emp = Employee.start(r.sop_id, r.goal, BASE_URL, r.scenario)
        except (ValueError, FileNotFoundError) as e:
            raise HTTPException(422, str(e))
        _spawn(emp)
        return {"id": emp.run.id}


@app.get("/api/runs")
def runs():
    return list_runs()


@app.get("/api/runs/{rid}")
def get_run(rid: str):
    try:
        d = Run.load(rid).__dict__.copy()
    except FileNotFoundError:
        raise HTTPException(404, "no such run")
    d.pop("observations", None)
    d["alive"] = rid in _active and _active[rid].is_alive()
    return d


class AnswerReq(BaseModel):
    response: str = ""
    approve: bool | None = None


@app.post("/api/runs/{rid}/answer")
def answer(rid: str, r: AnswerReq):
    with _lock:
        run = Run.load(rid)
        if run.status != "waiting_human":
            raise HTTPException(409, f"run is {run.status}, not waiting")
        emp = Employee(run, BASE_URL)
        run.status = "running"   # claimed; the worker thread applies the answer
        run.save()
        run.status = "waiting_human"

        def work():  # Playwright objects are thread-bound: answer and loop share one thread
            emp.answer(r.response, r.approve)
            emp.loop()
        t = threading.Thread(target=work, daemon=True)
        _active[rid] = t
        t.start()
        return {"ok": True}


@app.post("/api/runs/{rid}/resume")
def resume(rid: str):
    """Continue a run that stopped on an infrastructure error (e.g. model outage)."""
    with _lock:
        run = Run.load(rid)
        if run.status != "failed":
            raise HTTPException(409, "only failed runs can be resumed")
        run.status, run.error = "running", None
        run.save()
        _spawn(Employee(run, BASE_URL))
        return {"ok": True}


@app.post("/api/runs/{rid}/review")
def review(rid: str):
    try:
        return manager.review_run(Run.load(rid))
    except Exception as e:
        raise HTTPException(422, str(e))


@app.get("/runs/{rid}/shots/{name}")
def shot(rid: str, name: str):
    p = (RUNS_DIR / rid / "shots" / name).resolve()
    if p.parent != (RUNS_DIR / rid / "shots").resolve() or not p.exists():
        raise HTTPException(404)
    return FileResponse(p)


@app.get("/api/world")
def world_state():
    return world.snapshot()
