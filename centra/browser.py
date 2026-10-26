"""Real browser via Playwright. The model sees a text snapshot of the page where
every interactive element has an id like [e4]; it acts by id, not by CSS selector."""
from __future__ import annotations

import os

from playwright.sync_api import Error as PWError, sync_playwright

SNAPSHOT_JS = r"""
() => {
  let n = 0; const out = [];
  const label = el => {
    const id = el.id && document.querySelector(`label[for="${el.id}"]`);
    const wrap = el.closest('label');
    return ((id && id.innerText) || (wrap && wrap.childNodes[0] && wrap.childNodes[0].textContent) ||
            el.getAttribute('name') || '').trim();
  };
  const walk = node => {
    if (node.nodeType === 3) { const t = node.textContent.trim(); if (t) out.push(t); return; }
    if (node.nodeType !== 1) return;
    const el = node, tag = el.tagName.toLowerCase();
    if (['script', 'style', 'option'].includes(tag)) return;
    if (['a', 'button', 'select', 'textarea', 'input'].includes(tag)) {
      const cid = 'e' + (++n); el.setAttribute('data-cid', cid);
      if (tag === 'a') out.push(`[${cid} link "${el.innerText.trim()}" -> ${el.getAttribute('href')}]`);
      else if (tag === 'button') {
        const act = el.dataset.action ? ` (write: ${el.dataset.action}, subject ${el.dataset.subject})` : '';
        out.push(`[${cid} button "${el.innerText.trim()}"${act}]`);
      } else if (tag === 'select') {
        const opts = [...el.options].map(o => o.value + (o.selected ? '*' : '')).join(', ');
        out.push(`[${cid} select "${label(el)}" options: ${opts}]`);
      } else if (tag === 'textarea') out.push(`[${cid} textarea "${label(el)}" value: "${el.value}"]`);
      else if (el.type === 'checkbox') out.push(`[${cid} checkbox "${el.parentElement.innerText.trim()}" ${el.checked ? 'checked' : 'unchecked'}]`);
      else out.push(`[${cid} input "${label(el)}" value: "${el.value}"]`);
      if (tag !== 'a' && tag !== 'button') return;
      return;
    }
    for (const c of el.childNodes) walk(c);
    if (['p', 'div', 'tr', 'h1', 'h2', 'h3', 'li', 'blockquote', 'table', 'nav', 'form'].includes(tag)) out.push('\n');
  };
  walk(document.body);
  return out.join(' ').replace(/ *\n[ \n]*/g, '\n').trim();
}
"""

FORM_STATE_JS = r"""
el => {
  const f = el.closest('form'); const vals = {};
  if (f) for (const i of f.querySelectorAll('select, textarea, input')) {
    if (i.type === 'checkbox') vals[i.name] = i.checked; else vals[i.name] = i.value;
  }
  return {action: el.dataset.action || null, subject: el.dataset.subject || null, form: vals};
}
"""


class Browser:
    def __init__(self, shots_dir):
        self.shots_dir = shots_dir
        self._pw = None
        self.page = None
        self.shot_n = 0

    def _ensure(self):
        if self.page is None:
            self._pw = sync_playwright().start()
            headless = os.environ.get("HEADED", "") != "1"
            self._b = self._pw.chromium.launch(headless=headless)
            self.page = self._b.new_page(viewport={"width": 1100, "height": 800})
            self.page.set_default_timeout(int(os.environ.get("BROWSER_TIMEOUT_MS", "8000")))

    def close(self):
        if self._pw:
            try:
                self._b.close()
                self._pw.stop()
            except PWError:
                pass
            self._pw = self.page = None

    def goto(self, url: str) -> str:
        self._ensure()
        resp = self.page.goto(url, wait_until="domcontentloaded")
        if resp and resp.status >= 400:
            raise RuntimeError(f"HTTP {resp.status} at {url}: {self.page.inner_text('body')[:200]}")
        return self.snapshot()

    def snapshot(self) -> str:
        self._ensure()
        try:  # live view for the operator console; overwritten on every page change
            self.page.screenshot(path=str(self.shots_dir / "live.jpg"), type="jpeg", quality=70)
        except PWError:
            pass
        return f"URL: {self.page.url}\nTITLE: {self.page.title()}\n" + self.page.evaluate(SNAPSHOT_JS)

    def locate(self, cid: str):
        self._ensure()
        loc = self.page.locator(f'[data-cid="{cid}"]')
        if loc.count() == 0:
            raise LookupError(f"element {cid} not found on the current page; call browser_read for fresh ids")
        return loc.first

    def inspect(self, cid: str) -> dict:
        return self.locate(cid).evaluate(FORM_STATE_JS)

    def click(self, cid: str) -> str:
        loc = self.locate(cid)
        navigates = loc.evaluate("e => e.tagName === 'BUTTON' || (e.tagName === 'A' && !!e.getAttribute('href'))")
        try:
            with self.page.expect_navigation(wait_until="domcontentloaded", timeout=4000) if navigates else _null():
                loc.click()
        except PWError as e:
            if "Timeout" not in str(e):
                raise
        return self.snapshot()

    def fill(self, cid: str, value: str) -> str:
        loc = self.locate(cid)
        kind = loc.evaluate("e => e.tagName.toLowerCase() + ':' + (e.type || '')")
        if kind.startswith("select"):
            loc.select_option(value)
        elif kind.endswith("checkbox"):
            loc.set_checked(str(value).lower() in ("true", "1", "yes", "checked"))
        else:
            loc.fill(value)
        return self.snapshot()

    def submit_recorded(self, url: str, action: str, subject: str, form: dict) -> str:
        """Replay an approved browser write exactly as it was gated."""
        self.goto(url)
        btn = self.page.locator(f'button[data-action="{action}"][data-subject="{subject}"]').first
        f = btn.locator("xpath=ancestor::form")
        for name, val in form.items():
            field = f.locator(f'[name="{name}"]').first
            tag = field.evaluate("e => e.tagName.toLowerCase() + ':' + (e.type || '')")
            if tag.startswith("select"):
                field.select_option(val)
            elif tag.endswith("checkbox"):
                field.set_checked(bool(val))
            else:
                field.fill(str(val))
        with self.page.expect_navigation(wait_until="domcontentloaded"):
            btn.click()
        return self.snapshot()

    def screenshot(self, label: str) -> str | None:
        if self.page is None:
            return None
        self.shot_n += 1
        name = f"{self.shot_n:02d}-{label}.png"
        self.page.screenshot(path=str(self.shots_dir / name), full_page=True)
        return name


class _null:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False
