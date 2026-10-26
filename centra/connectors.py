"""Typed clients for the company's systems of record. Used by tools (to act),
by the policy gate (to look up truth, e.g. a subject's real email) and by the
verifier (to check outcomes). In production these would be Odoo/Zendesk/Gmail."""
from __future__ import annotations

import httpx


class WriteUnknown(Exception):
    """The request may or may not have been committed (timeout / 5xx after send)."""


class Company:
    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/") + "/sandbox"
        self.http = httpx.Client(timeout=15, trust_env=False)

    def _get(self, path: str):
        r = self.http.get(self.base + path)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, body: dict, key: str):
        try:
            r = self.http.post(self.base + path, json=body, headers={"Idempotency-Key": key})
        except httpx.TimeoutException as e:
            raise WriteUnknown(f"timeout: {e}")
        if r.status_code >= 500:
            raise WriteUnknown(f"HTTP {r.status_code}: {r.text[:150]}")
        if r.status_code >= 400:
            raise ValueError(f"HTTP {r.status_code}: {r.json().get('detail', r.text)}")
        return r.json()

    # reads
    def applicants(self):
        return self._get("/api/ats/applicants")

    def tickets(self):
        return self._get("/api/helpdesk/tickets")

    def events(self):
        return self._get("/api/calendar/events")

    def free_slots(self):
        return self._get("/api/calendar/free")

    def outbox(self):
        return self._get("/api/mail/outbox")

    # writes (always idempotent)
    def book(self, slot: str, attendee: str, title: str, key: str):
        return self._post("/api/calendar/events", {"slot": slot, "attendee": attendee, "title": title}, key)

    def send(self, to: str, subject: str, body: str, key: str):
        return self._post("/api/mail/send", {"to": to, "subject": subject, "body": body}, key)

    def scope(self, scope: dict) -> dict[str, dict]:
        """Subjects the SOP covers right now, straight from the system of record."""
        flt = scope.get("filter", {})

        def match(row):
            return all(str(row.get(k, "")).strip().lower() == str(v).strip().lower() for k, v in flt.items())
        if scope["source"] == "ats":
            rows = [a for a in self.applicants() if match(a)]
            return {a["id"]: {"name": a["name"], "email": a["email"]} for a in rows}
        rows = [t for t in self.tickets() if match(t)]
        return {t["id"]: {"name": t["from"].split("@")[0].title(), "email": t["from"]} for t in rows}
