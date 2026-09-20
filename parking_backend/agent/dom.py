"""Turning a live page into an Observation.

Two properties matter and both come from here. The model addresses elements only by a `nid` this module
hands out, wiped and reissued on every harvest, so a stale or invented reference cannot resolve. And card
fields are never harvested at all, so there is no nid by which the model could reach one.
"""
from __future__ import annotations

from parking_backend.agent.secrets import REDACTED, SecretVault, redact
from parking_backend.agent.types import Node, Observation, StaleNode


MAX_NODES = 120
MAX_TEXT = 4000
MAX_OPTIONS = 24

# Anything matching these is a payment field: it is masked in the digest and never given a nid, so the only
# route to one is FILL_SECRET, which resolves it here rather than from a model-supplied reference.
CARD_SELECTOR = ", ".join((
  "[autocomplete^='cc-']", "input[type='password']", "[name*='card' i]", "[id*='card' i]",
  "[name*='cvv' i]", "[id*='cvv' i]", "[name*='cvc' i]", "[id*='cvc' i]",
))

INTERACTIVE = ", ".join((
  "a[href]", "button", "input", "select", "textarea", "[role='button']", "[role='link']", "[role='radio']",
  "[role='checkbox']", "[role='combobox']", "[onclick]", "[tabindex]:not([tabindex='-1'])",
))

# Runs in the page. Returns the node list and marks each kept element with data-pa-nid.
HARVEST_JS = """
const MAX = arguments[0], CARD = arguments[1], INTERACTIVE = arguments[2];
document.querySelectorAll('[data-pa-nid]').forEach(e => e.removeAttribute('data-pa-nid'));
const cards = new Set(Array.from(document.querySelectorAll(CARD)));
const visible = (e) => {
  if (!e.getClientRects().length) return false;
  const s = window.getComputedStyle(e);
  return s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0';
};
const label = (e) => {
  let t = e.getAttribute('aria-label') || e.getAttribute('placeholder') || '';
  if (!t && e.labels && e.labels.length) t = e.labels[0].innerText || '';
  if (!t && e.id) { const l = document.querySelector(`label[for="${CSS.escape(e.id)}"]`); if (l) t = l.innerText || ''; }
  if (!t) t = (e.innerText || e.value || e.getAttribute('name') || e.getAttribute('title') || '');
  return (t || '').replace(/\\s+/g, ' ').trim().slice(0, 120);
};
const out = [];
let n = 0;
for (const e of document.querySelectorAll(INTERACTIVE)) {
  if (out.length >= MAX) break;
  if (!visible(e) || cards.has(e)) continue;
  const nid = 'n' + (++n);
  e.setAttribute('data-pa-nid', nid);
  const tag = e.tagName.toLowerCase();
  const node = {
    nid: nid,
    role: e.getAttribute('role') || (tag === 'a' ? 'link' : tag === 'input' ? (e.type || 'textbox') : tag),
    name: label(e),
    value: (e.value == null ? '' : String(e.value)).slice(0, 120),
    input_type: (e.type || ''),
    enabled: !(e.disabled || e.getAttribute('aria-disabled') === 'true'),
    options: [],
  };
  if (tag === 'select') {
    node.options = Array.from(e.options).slice(0, arguments[3]).map(o => (o.text || '').trim().slice(0, 64));
  }
  out.push(node);
}
const body = document.body ? (document.body.innerText || '') : '';
return {nodes: out, text: body.slice(0, arguments[4]), title: document.title || '', url: location.href};
"""

# Hide payment fields and every iframe before a screenshot, then put them back.
HIDE_JS = """
const hidden = [];
document.querySelectorAll('iframe, ' + arguments[0]).forEach(e => {
  hidden.push([e, e.style.visibility]);
  e.style.visibility = 'hidden';
});
window.__paHidden = hidden;
return hidden.length;
"""
RESTORE_JS = "(window.__paHidden || []).forEach(([e, v]) => { e.style.visibility = v; }); window.__paHidden = null;"


def harvest(driver, step: int, *, vault: SecretVault | None = None,
            screenshot: bool = True) -> Observation:
  """Read the current page into an Observation, with card fields excluded and text scrubbed."""
  from urllib.parse import urlsplit

  raw = driver.execute_script(HARVEST_JS, MAX_NODES, CARD_SELECTOR, INTERACTIVE, MAX_OPTIONS, MAX_TEXT)
  nodes = tuple(
    Node(nid=str(item["nid"]), role=str(item.get("role") or ""), name=redact(str(item.get("name") or ""), vault),
         value=_safe_value(item, vault), input_type=str(item.get("input_type") or ""),
         enabled=bool(item.get("enabled", True)),
         options=tuple(str(option) for option in (item.get("options") or ())))
    for item in (raw.get("nodes") or ())
  )
  url = str(raw.get("url") or driver.current_url)
  return Observation(
    step=step, url=url, host=urlsplit(url).hostname or "", title=str(raw.get("title") or ""),
    nodes=nodes, text_digest=redact(str(raw.get("text") or ""), vault),
    screenshot_jpeg=capture(driver) if screenshot else None,
    hints=detect_hints(str(raw.get("text") or ""), nodes),
  )


def _safe_value(item: dict, vault: SecretVault | None) -> str:
  """A field the harvester kept can still hold something sensitive the driver typed."""
  value = str(item.get("value") or "")
  if not value:
    return ""
  digits = "".join(c for c in value if c.isdigit())
  if len(digits) >= 12:
    return REDACTED
  return redact(value, vault)


def capture(driver) -> bytes | None:
  """A screenshot with payment fields and iframes blanked. Nothing else may screenshot the page."""
  try:
    driver.execute_script(HIDE_JS, CARD_SELECTOR)
  except Exception:
    return None  # better no screenshot than one that might show a card
  try:
    return driver.get_screenshot_as_png()
  except Exception:
    return None
  finally:
    try:
      driver.execute_script(RESTORE_JS)
    except Exception:
      pass


def detect_hints(text: str, nodes: tuple[Node, ...]) -> tuple[str, ...]:
  """Deterministic notes about the screen, so the model is not the only thing that can spot a blocker."""
  lowered = text.lower()
  hints = []
  if any(word in lowered for word in ("recaptcha", "i'm not a robot", "verify you are human", "captcha")):
    hints.append("captcha_present")
  if any(word in lowered for word in ("download the app", "open in app", "get the app", "continue in app")):
    hints.append("app_interstitial")
  if any(word in lowered for word in ("sign in", "log in", "create an account", "register")):
    hints.append("account_prompt")
  if any(word in lowered for word in ("verification code", "one-time code", "we texted", "sent you a code")):
    hints.append("otp_prompt")
  if any(word in lowered for word in ("accept cookies", "cookie preferences", "we use cookies")):
    hints.append("cookie_banner")
  if any("card" in node.name.lower() or node.input_type == "password" for node in nodes):
    hints.append("payment_fields_present")
  return tuple(hints)


def resolve(driver, nid: str):
  """Find the element a nid refers to. Only ever called with a nid from the current observation."""
  from selenium.common.exceptions import NoSuchElementException, StaleElementReferenceException
  from selenium.webdriver.common.by import By
  try:
    return driver.find_element(By.CSS_SELECTOR, f'[data-pa-nid="{nid}"]')
  except (NoSuchElementException, StaleElementReferenceException) as exc:
    raise StaleNode(f"{nid} no longer exists on the current page") from exc
