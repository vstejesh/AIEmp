"""Independent verifier. Runs after the employee says 'finish'. It ignores the
employee's summary and reads the systems of record directly:
  - every subject in scope (snapshotted by code at run start) was decided,
  - each decision re-derives from the recorded facts under the SOP rules,
  - every required action exists in the real system exactly once,
  - no action outside the decision happened, and nothing out of scope was touched."""
from __future__ import annotations

from datetime import datetime

import httpx

from .connectors import Company
from .policy import DAYS
from .sop import SOP, evaluate
from .state import Run


def verify(sop: SOP, run: Run, co: Company) -> dict:
    checks: list[dict] = []

    def check(subject, name, ok, detail=""):
        checks.append({"subject": subject, "check": name, "ok": bool(ok), "detail": detail})

    events, outbox = co.events(), co.outbox()
    world = httpx.get(co.base + "/admin/state", timeout=10, trust_env=False).json()
    apps = {a["id"]: a for a in world["applicants"]}
    tickets = {t["id"]: t for t in world["tickets"]}

    for sid, contact in run.scope.items():
        dec = run.decisions.get(sid)
        if not dec:
            check(sid, "processed", False, "no decision recorded")
            continue
        outcome, why = evaluate(sop, run.subject_facts(sid))
        check(sid, "decision follows SOP rules", outcome == dec["outcome"], f"{dec['outcome']} ({why})")
        expected = set(sop.outcomes[dec["outcome"]])
        email = contact["email"]
        my_events = [e for e in events if e["attendee"] == email]

        for action in sorted(expected):
            if action.startswith("ats.set_status="):
                want = action.split("=", 1)[1]
                got = apps.get(sid, {}).get("status")
                check(sid, action, got == want, f"ATS status is '{got}'")
            elif action == "calendar.book":
                ok = len(my_events) == 1
                detail = f"{len(my_events)} event(s)"
                c = sop.constraints.get("calendar.book")
                if ok and c:
                    dt = datetime.fromisoformat(my_events[0]["slot"])
                    ok = (not c.days or DAYS[dt.weekday()] in c.days) and (not c.hours or dt.hour in c.hours)
                    detail += f" at {my_events[0]['slot']}" + ("" if ok else " (violates SOP constraint)")
                check(sid, action, ok, detail)
            elif action.startswith("mail.send="):
                t = sop.templates[action.split("=", 1)[1]]
                to = email if t.to == "subject" else sop.owner
                subj = t.subject.replace("{name}", contact["name"]).replace("{subject_id}", sid)
                n = sum(1 for m in outbox if m["to"] == to and m["subject"] == subj)
                check(sid, action, n == 1, f"{n} message(s) to {to} titled '{subj}'")
            elif action.startswith("helpdesk.reply"):
                t = tickets.get(sid, {})
                want_closed = action.endswith("+close")
                ok = len(t.get("replies", [])) >= 1 and (t.get("status") == "closed") == want_closed
                check(sid, action, ok, f"{len(t.get('replies', []))} repl(ies), status '{t.get('status')}'")
            elif action.startswith("helpdesk.tag="):
                tag = action.split("=", 1)[1]
                check(sid, action, tag in tickets.get(sid, {}).get("tags", []), f"tags {tickets.get(sid, {}).get('tags')}")

        # Negative checks: nothing the decision did not call for.
        if "calendar.book" not in expected:
            check(sid, "no interview booked", not my_events, f"{len(my_events)} event(s)")
        for name, t in sop.templates.items():
            if f"mail.send={name}" not in expected and t.to == "subject":
                n = sum(1 for m in outbox if m["to"] == email and m["subject"] == t.subject.replace("{name}", contact["name"]))
                if n:
                    check(sid, f"no '{name}' email", False, f"{n} sent")
        if sop.scope["source"] == "helpdesk" and not any(a.startswith("helpdesk.reply") for a in expected):
            n = len(tickets.get(sid, {}).get("replies", []))
            check(sid, "no customer reply", n == 0, f"{n} repl(ies)")

    in_scope = set(run.scope) | {c["email"] for c in run.scope.values()} | {sop.owner}
    stray = [w for w in world["write_log"] if w["subject"] not in in_scope]
    check("*", "nothing outside scope was changed", not stray, f"{len(stray)} stray write(s): {stray[:3]}")

    return {"passed": all(c["ok"] for c in checks), "checks": checks,
            "failed": [c for c in checks if not c["ok"]]}
