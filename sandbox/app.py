"""The simulated company: an ATS and a helpdesk (HTML, driven by a real browser),
plus calendar and mail services (JSON APIs). Mounted at /sandbox.

Write buttons carry data-action / data-subject attributes. That is how the policy
gate knows what a click will actually do, instead of trusting the model's intent."""
from __future__ import annotations

import html
import time
from pathlib import Path

from fastapi import APIRouter, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel

from . import scenarios, world
from .world import LOCK, STATE

router = APIRouter()
RESUMES = Path(__file__).parent / "data" / "resumes"

CSS = """<style>body{font-family:system-ui,sans-serif;margin:24px;max-width:900px}
nav a{margin-right:14px}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccc;
padding:6px;text-align:left}.tab-active{font-weight:bold}form{margin:12px 0}</style>"""


def _page(title: str, body: str) -> HTMLResponse:
    if (ms := world.chaos("slow_ms")):
        time.sleep(ms / 1000)
    return HTMLResponse(f"<!doctype html><html><head><title>{html.escape(title)}</title>{CSS}</head>"
                        f"<body>{body}</body></html>")


def _write(kind: str, **data) -> None:
    STATE["write_log"].append({"kind": kind, "at": time.time(), **data})


# ---------------------------------------------------------------- ATS (HTML)
TABS = [("new", "New"), ("interview", "Interview"), ("rejected", "Rejected")]


@router.get("/ats", response_class=HTMLResponse)
def ats_home(tab: str = ""):
    label_new = world.chaos("ats_new_tab_label", "New")
    labels = {"new": label_new, "interview": "Interview", "rejected": "Rejected"}
    nav = " ".join(f'<a class="{"tab-active" if k == tab else ""}" href="/sandbox/ats?tab={k}">{labels[k]}</a>'
                   for k, _ in TABS)
    body = f"<h1>Acme ATS</h1><nav>{nav}</nav>"
    if not tab:
        body += "<p>Select a tab to see applicants.</p>"
    else:
        rows = "".join(
            f'<tr><td>{a["id"]}</td><td><a href="/sandbox/ats/applicant/{a["id"]}">{html.escape(a["name"])}</a></td>'
            f'<td>{a["role"]}</td><td>{a["applied"]}</td></tr>'
            for a in STATE["applicants"] if a["status"] == tab)
        body += (f"<h2>{labels[tab]}</h2><table><tr><th>ID</th><th>Name</th><th>Role</th><th>Applied</th></tr>"
                 f"{rows or '<tr><td colspan=4>No applicants</td></tr>'}</table>")
    return _page("ATS", body)


@router.get("/ats/applicant/{aid}", response_class=HTMLResponse)
def ats_applicant(aid: str):
    a = world.applicant(aid)
    if not a:
        raise HTTPException(404, "No such applicant")
    opts = "".join(f'<option value="{k}" {"selected" if k == a["status"] else ""}>{v}</option>' for k, v in TABS)
    body = f"""<h1>{html.escape(a['name'])}</h1><p><a href="/sandbox/ats?tab={a['status']}">Back</a></p>
    <table><tr><th>Applicant ID</th><td>{a['id']}</td></tr>
    <tr><th>Email</th><td>{a['email']}</td></tr><tr><th>Role</th><td>{a['role']}</td></tr>
    <tr><th>Referral</th><td>{'Yes' if a['referral'] else 'No'}</td></tr>
    <tr><th>Resume file</th><td>{a['resume']}</td></tr><tr><th>Status</th><td>{a['status']}</td></tr></table>
    <form method="post" action="/sandbox/ats/applicant/{a['id']}/status">
      <label>New status <select name="status">{opts}</select></label>
      <button type="submit" data-action="ats.set_status" data-subject="{a['id']}">Save status</button>
    </form>"""
    return _page(a["name"], body)


@router.post("/ats/applicant/{aid}/status")
def ats_set_status(aid: str, status: str = Form(...)):
    with LOCK:
        a = world.applicant(aid)
        if not a or status not in dict(TABS):
            raise HTTPException(400, "Bad request")
        a["status"] = status
        _write("ats.set_status", subject=aid, value=status)
    return RedirectResponse(f"/sandbox/ats/applicant/{aid}?saved=1", status_code=303)


@router.get("/files/{name}", response_class=PlainTextResponse)
def file(name: str):
    name = STATE.get("resume_override", {}).get(name, name)
    p = (RESUMES / name).resolve()
    if p.parent != RESUMES.resolve() or not p.exists():
        raise HTTPException(404, "File not found")
    return p.read_text()


# ---------------------------------------------------------------- Calendar (API)
class Booking(BaseModel):
    slot: str
    attendee: str
    title: str


@router.get("/api/calendar/free")
def free_slots():
    taken = set(STATE["calendar"]["busy"]) | {e["slot"] for e in STATE["calendar"]["events"]}
    return [s for s in world.all_slots() if s not in taken]


@router.get("/api/calendar/events")
def events():
    return STATE["calendar"]["events"]


@router.post("/api/calendar/events")
def book(b: Booking, idempotency_key: str = Header(...)):
    with LOCK:
        if idempotency_key in STATE["idempotency"]:
            return STATE["idempotency"][idempotency_key]
        if b.slot not in world.all_slots():
            raise HTTPException(400, "Not a bookable slot")
        if world.consume_chaos("calendar_steal_slot"):
            STATE["calendar"]["busy"].append(b.slot)   # someone else grabbed it just now
        taken = set(STATE["calendar"]["busy"]) | {e["slot"] for e in STATE["calendar"]["events"]}
        if b.slot in taken:
            raise HTTPException(409, "Slot no longer available")
        ev = {"id": f"EV-{len(STATE['calendar']['events']) + 1:03d}", **b.model_dump()}
        STATE["calendar"]["events"].append(ev)
        STATE["idempotency"][idempotency_key] = ev
        _write("calendar.book", subject=b.attendee, value=b.slot)
        if world.consume_chaos("calendar_lost_response"):
            # Committed, but the client never hears back.
            return JSONResponse({"error": "upstream timeout"}, status_code=504)
        return ev


# ---------------------------------------------------------------- Mail (API)
class Mail(BaseModel):
    to: str
    subject: str
    body: str


@router.get("/api/mail/outbox")
def outbox():
    return STATE["outbox"]


@router.post("/api/mail/send")
def send(m: Mail, idempotency_key: str = Header(...)):
    with LOCK:
        if idempotency_key in STATE["idempotency"]:
            return STATE["idempotency"][idempotency_key]
        msg = {"id": f"M-{len(STATE['outbox']) + 1:03d}", **m.model_dump()}
        STATE["outbox"].append(msg)
        STATE["idempotency"][idempotency_key] = msg
        _write("mail.send", subject=m.to, value=m.subject)
        return msg


# ---------------------------------------------------------------- Helpdesk (HTML)
@router.get("/helpdesk", response_class=HTMLResponse)
def helpdesk(status: str = "open"):
    rows = "".join(f'<tr><td>{t["id"]}</td><td><a href="/sandbox/helpdesk/ticket/{t["id"]}">'
                   f'{html.escape(t["subject"])}</a></td><td>{t["from"]}</td></tr>'
                   for t in STATE["tickets"] if t["status"] == status)
    nav = '<nav><a href="/sandbox/helpdesk?status=open">Open</a><a href="/sandbox/helpdesk?status=closed">Closed</a></nav>'
    return _page("Helpdesk", f"<h1>Acme Helpdesk</h1>{nav}<h2>{status.title()} tickets</h2><table>"
                 f"<tr><th>ID</th><th>Subject</th><th>From</th></tr>{rows or '<tr><td colspan=3>None</td></tr>'}</table>")


@router.get("/helpdesk/ticket/{tid}", response_class=HTMLResponse)
def ticket_page(tid: str):
    t = world.ticket(tid)
    if not t:
        raise HTTPException(404, "No such ticket")
    replies = "".join(f"<li>{html.escape(r)}</li>" for r in t["replies"]) or "<li>none</li>"
    body = f"""<h1>{t['id']}: {html.escape(t['subject'])}</h1><p><a href="/sandbox/helpdesk">Back</a></p>
    <table><tr><th>From</th><td>{t['from']}</td></tr><tr><th>Status</th><td>{t['status']}</td></tr>
    <tr><th>Tags</th><td>{', '.join(t['tags']) or 'none'}</td></tr></table>
    <h3>Message</h3><blockquote>{html.escape(t['body'])}</blockquote><h3>Replies</h3><ul>{replies}</ul>
    <form method="post" action="/sandbox/helpdesk/ticket/{t['id']}/reply">
      <label>Reply text <textarea name="text" rows="4" cols="60"></textarea></label>
      <label><input type="checkbox" name="close" value="1"> Close ticket after reply</label>
      <button type="submit" data-action="helpdesk.reply" data-subject="{t['id']}">Send reply</button>
    </form>
    <form method="post" action="/sandbox/helpdesk/ticket/{t['id']}/tag">
      <label>Tag <select name="tag"><option value="finance">finance</option><option value="engineering">engineering</option>
      <option value="triage">triage</option></select></label>
      <button type="submit" data-action="helpdesk.tag" data-subject="{t['id']}">Add tag</button>
    </form>"""
    return _page(t["id"], body)


@router.post("/helpdesk/ticket/{tid}/reply")
def ticket_reply(tid: str, text: str = Form(...), close: str = Form("")):
    with LOCK:
        t = world.ticket(tid)
        if not t or not text.strip():
            raise HTTPException(400, "Bad request")
        t["replies"].append(text.strip())
        if close:
            t["status"] = "closed"
        _write("helpdesk.reply", subject=tid, value=text.strip(), close=bool(close))
    return RedirectResponse(f"/sandbox/helpdesk/ticket/{tid}?saved=1", status_code=303)


@router.post("/helpdesk/ticket/{tid}/tag")
def ticket_tag(tid: str, tag: str = Form(...)):
    with LOCK:
        t = world.ticket(tid)
        if not t:
            raise HTTPException(400, "Bad request")
        if tag not in t["tags"]:
            t["tags"].append(tag)
        _write("helpdesk.tag", subject=tid, value=tag)
    return RedirectResponse(f"/sandbox/helpdesk/ticket/{tid}?saved=1", status_code=303)


# ---------------------------------------------------------------- System-of-record reads + admin
@router.get("/api/ats/applicants")
def api_applicants():
    return STATE["applicants"]


@router.get("/api/helpdesk/tickets")
def api_tickets():
    return STATE["tickets"]


@router.post("/admin/reset/{scenario}")
def admin_reset(scenario: str):
    if scenario not in scenarios.ALL:
        raise HTTPException(404, f"Unknown scenario; choose from {scenarios.ALL}")
    world.reset(scenarios.get(scenario)["seed"])
    return {"reset": scenario}


@router.get("/admin/state")
def admin_state(request: Request):
    return world.snapshot()
