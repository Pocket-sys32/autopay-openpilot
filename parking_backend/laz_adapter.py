from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import os
import re
import time

import requests

from parking_backend.appium_adapter import CHROMEDRIVER_PATH, FormChanged, SubmissionUnknown
from parking_backend.errors import CaptchaChallenged, PaymentDeclined, ReservationRejected


LOCATION_ID = "143245"
ENTRY_URL = f"https://clip.lazparking.com/p/{LOCATION_ID}"
CHECKOUT_HOST = "go.lazparking.com"
SITE_CODE = "CA1231"
SUPPORTED_DURATION_SECONDS = 3 * 3600  # LAZ's shortest stay at this site
# Observed on a real manual checkout with an underfunded card (toast, about 3 s after PAY):
#   "Payment failed. Your credit card was not authorized. Please try a different payment method or contact your
#    card issuer. Not sufficient funds 108-001"
DECLINE_MARKERS = ("declined", "unsuccessful", "could not be processed", "card was not", "payment failed",
                   "try another card", "not authorized", "not sufficient funds")
# LAZ's checkout shows this toast and stays on the form. The card issuer reported no decline, so this is a
# rejection of the reservation itself, not a card decline.
RESERVATION_MARKERS = ("could not validate reservation",)
PRICE_RE = re.compile(r"PAY\s*\$\s*(\d+(?:\.\d{2})?)")
# Pages loaded before the checkout so it is not reached from a browser with an empty cookie jar. google.com is
# the one that matters: that is where a signed-in session's cookies live, and reCAPTCHA reads them from its own
# iframe. LAZ's own homepage is where a person would normally start, and it sets the site's first-party cookies.
DEFAULT_WARMUP_URLS = ("https://www.google.com/", "https://www.wikipedia.org/", "https://www.lazparking.com/")
# Set by a signed-in Google session. Their presence is recorded; their values never are.
GOOGLE_SESSION_COOKIES = frozenset({"SID", "SSID", "HSID", "SAPISID", "APISID", "__Secure-1PSID", "__Secure-3PSID"})
# reCAPTCHA always embeds an anchor iframe, even when it lets the visitor through. Only the bframe, and only
# while it is actually visible, means a challenge is on screen.
RECAPTCHA_CHALLENGE_JS = """
return [...document.querySelectorAll('iframe')].some(f => {
  if (!/recaptcha\\/(api2|enterprise)\\/bframe/.test(f.src || '')) return false;
  const box = f.getBoundingClientRect();
  if (!box.width || !box.height) return false;
  for (let e = f; e; e = e.parentElement) {
    const s = getComputedStyle(e);
    if (s.visibility === 'hidden' || s.display === 'none' || s.opacity === '0') return false;
  }
  return true;
});
"""


class PriceLimitExceeded(FormChanged):
  pass


@dataclass(frozen=True, slots=True)
class LazProfile:
  email: str
  mobile: str
  street: str
  zip_code: str
  plate_state: str = "California"
  max_total_minor: int = 3000

  @classmethod
  def from_environment(cls) -> LazProfile:
    def need(name: str) -> str:
      value = os.getenv(name, "").strip()
      if not value:
        raise RuntimeError(f"{name} is required for the LAZ provider")
      return value
    return cls(need("PARKING_LAZ_EMAIL"), need("PARKING_LAZ_MOBILE"), need("PARKING_LAZ_STREET"),
               need("PARKING_LAZ_ZIP"), os.getenv("PARKING_LAZ_PLATE_STATE", "California"),
               int(os.getenv("PARKING_LAZ_MAX_TOTAL_MINOR", "3000")))


def split_expiry(expiration: str) -> tuple[str, str]:
  """'09/31', '0931' or '092031' -> ('09', '2031'). The checkout has separate MM and YYYY boxes."""
  digits = re.sub(r"\D", "", expiration)
  if len(digits) == 4:
    month, year = digits[:2], "20" + digits[2:]
  elif len(digits) == 6:
    month, year = digits[:2], digits[2:]
  else:
    raise ValueError("card expiry must look like MM/YY or MM/YYYY")
  if not 1 <= int(month) <= 12:
    raise ValueError("card expiry month is invalid")
  return month, year


def parse_pay_total_minor(page_text: str) -> int:
  match = PRICE_RE.search(page_text)
  if match is None:
    raise FormChanged("could not read the total from the pay button")
  dollars, _, cents = match.group(1).partition(".")
  return int(dollars) * 100 + int((cents or "0").ljust(2, "0"))


def is_decline(page_text: str) -> bool:
  lowered = page_text.lower()
  return any(marker in lowered for marker in DECLINE_MARKERS)


class LazAdapter:
  """Appium adapter for one allowlisted LAZ location, driven through a real Android Chrome."""

  def __init__(self, appium_url: str, *, card_number: str, cvv: str, expiration: str, profile: LazProfile,
               flaresolverr_url: str | None = None):
    self.appium_url = appium_url
    self.card_number = card_number
    self.cvv = cvv
    self.expiration = expiration
    self.profile = profile
    self.flaresolverr_url = flaresolverr_url or os.getenv("FLARESOLVERR_URL")
    # An empty PARKING_LAZ_WARMUP_URLS turns warming off; the budget caps it so a slow site cannot eat the
    # worker's deadline.
    configured = os.getenv("PARKING_LAZ_WARMUP_URLS")
    self.warmup_urls = (tuple(u.strip() for u in configured.split(",") if u.strip())
                        if configured is not None else DEFAULT_WARMUP_URLS)
    self.warmup_budget = int(os.getenv("PARKING_LAZ_WARMUP_BUDGET", "30"))
    self.attach_to_chrome = os.getenv("PARKING_LAZ_ATTACH_CHROME", "1") == "1"

  def validate_location(self, location_id: str) -> bool:
    return location_id == LOCATION_ID

  def get_quote(self, *, location_id: str, plate: str, duration_seconds: int) -> dict[str, object]:
    if not self.validate_location(location_id) or duration_seconds != SUPPORTED_DURATION_SECONDS:
      raise ValueError("unsupported LAZ location or duration")
    return {"location_id": location_id, "plate": plate, "duration_seconds": duration_seconds, "currency": "USD"}

  def lookup_session(self, attempt_id: str) -> dict[str, object] | None:
    return None

  def _solve_cloudflare(self, url: str) -> dict[str, object]:
    """Ask a FlareSolverr service (POST /v1) to load `url` and return its solution (cookies, user agent)."""
    try:
      response = requests.post(f"{self.flaresolverr_url.rstrip('/')}/v1", timeout=75,
                               json={"cmd": "request.get", "url": url, "maxTimeout": 60000})
      body = response.json()
    except (requests.RequestException, ValueError) as exc:
      raise FormChanged(f"flaresolverr request failed: {type(exc).__name__}") from exc
    if body.get("status") != "ok":
      raise FormChanged(f"flaresolverr did not solve the challenge: {body.get('message', 'unknown')}")
    return body["solution"]

  def submit(self, request: dict[str, object], *, mark_submitting: Callable[[], None]) -> dict[str, object]:
    from appium import webdriver
    from appium.options.android import UiAutomator2Options
    from selenium.webdriver.common.by import By

    # cf_clearance is bound to the IP and user agent it was issued for, so the browser must present the solver's user agent.
    solution = (self._solve_cloudflare(ENTRY_URL)
                if self.flaresolverr_url and os.getenv("PARKING_LAZ_INJECT_CF_COOKIES") == "1" else None)
    options = UiAutomator2Options()
    options.platform_name = "Android"
    options.browser_name = "Chrome"
    options.automation_name = "UiAutomator2"
    options.set_capability("appium:newCommandTimeout", 150)
    options.set_capability("appium:noReset", True)
    options.set_capability("appium:chromedriverExecutable", CHROMEDRIVER_PATH)
    # Chromedriver otherwise leaves navigator.webdriver set, which reCAPTCHA reads and scores against.
    chrome_args = ["--disable-blink-features=AutomationControlled"]
    if solution and solution.get("userAgent"):
      chrome_args.append(f"--user-agent={solution['userAgent']}")
    chrome_options: dict[str, object] = {"args": chrome_args}
    # Left to itself, chromedriver relaunches Chrome with a cleared data directory, throwing away the signed-in
    # Google session before the first page loads — and that session is what reCAPTCHA reads. Attaching to the
    # running Chrome keeps the profile. The args above then do not apply, because Chrome is not restarted;
    # navigator.webdriver is false anyway, precisely because chromedriver did not launch it.
    # The two are mutually exclusive: matching the solver's user agent needs a relaunch, which wipes the
    # session, so the Cloudflare cookie path wins wherever it is switched on.
    attach = self.attach_to_chrome and solution is None
    if attach:
      chrome_options["androidUseRunningApp"] = True
      chrome_options["androidPackage"] = "com.android.chrome"
    options.set_capability("appium:chromeOptions", chrome_options)
    try:
      driver = webdriver.Remote(self.appium_url, options=options)
    except Exception as exc:
      if not attach:
        raise
      # Deliberately no fallback to a normal launch: that would succeed while silently signing the browser out.
      raise FormChanged("could not attach to a running Chrome; open it on the device and check it is signed in") from exc
    driver.set_page_load_timeout(30)
    try:
      self._stamp = time.strftime("%Y%m%dT%H%M%S")
      self._warm_up(driver)
      driver.get(ENTRY_URL)
      if solution:
        for cookie in solution.get("cookies", []):
          try:
            driver.add_cookie({"name": cookie["name"], "value": cookie["value"], "path": cookie.get("path", "/")})
          except Exception:
            pass  # cookies for other domains cannot be set on this page
        driver.get(ENTRY_URL)
      self._stamp = time.strftime("%Y%m%dT%H%M%S")
      for reload_left in (1, 0):  # Cloudflare's verification page sometimes sticks until the page is reloaded
        try:
          self._wait(lambda: driver.execute_script("return !!document.getElementById('buyNowSearch')"), "GO button", timeout=30)
          break
        except FormChanged:
          self._snapshot(driver, "0-no-go-button")
          if not reload_left:
            raise
          driver.get(ENTRY_URL)
      if SITE_CODE not in driver.find_element(By.TAG_NAME, "body").text:
        raise FormChanged("unexpected LAZ location")
      if urlparse_host(driver.current_url) != CHECKOUT_HOST:
        raise FormChanged("unexpected checkout host")
      driver.execute_script("document.getElementById('buyNowSearch').click()")  # default stay is the 3-hour minimum
      next_button = self._wait(lambda: driver.execute_script(
        "return [...document.querySelectorAll('button')].find(b=>b.innerText.trim()==='NEXT'&&b.offsetWidth)||null"), "NEXT button")
      driver.execute_script("arguments[0].click()", next_button)
      self._wait(lambda: driver.execute_script("return !!document.getElementById('parkerLicensePlate')"), "checkout form")
      time.sleep(3)  # let the checkout page finish loading before typing into it
      self._expected: dict[str, str] = {}
      self._stamp = time.strftime("%Y%m%dT%H%M%S")

      total = parse_pay_total_minor(driver.find_element(By.TAG_NAME, "body").text)
      if total > self.profile.max_total_minor:
        raise PriceLimitExceeded(f"total {total} exceeds the configured limit")
      # Choose the state first: changing it can re-render the plate field and wipe anything typed there.
      driver.execute_script(
        "const s=document.getElementById('parkerLicensePlateState');" +
        "const o=[...s.options].find(x=>x.text===arguments[0]); if(!o) return false;" +
        "Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set.call(s,o.value);" +
        "for (const t of ['input','change','blur']) s.dispatchEvent(new Event(t,{bubbles:true})); return true;",
        self.profile.plate_state
      ) or self._fail("plate state option not found")
      time.sleep(1)
      self._set(driver, "parkerLicensePlate", str(request["plate"]))
      for element_id, value in (("parkerFirstName", str(request["payer_first_name"])),
                                ("parkerLastName", str(request["payer_last_name"])),
                                ("parkerEmail", self.profile.email), ("parkerPhoneNumber", self.profile.mobile),
                                ("nameOnCard", str(request["name_on_card"])), ("ccAddress", self.profile.street),
                                ("ccZip", self.profile.zip_code)):
        self._set(driver, element_id, value)
      self._fill_card_frame(driver)

      pay = driver.execute_script(
        "return [...document.querySelectorAll('button')].find(b=>/^PAY \\$/.test(b.innerText.trim()))||null")
      if pay is None:
        raise FormChanged("pay button not found")
      time.sleep(2)  # give any late re-render a chance to clear a field before it is re-checked
      self._verify_fields(driver)
      # Stop here rather than clicking PAY into a challenge: nothing has been purchased yet at this point.
      if self._recaptcha_challenged(driver):
        self._snapshot(driver, "0-recaptcha-before-pay")
        raise CaptchaChallenged("reCAPTCHA challenged the checkout before payment")
      self._snapshot(driver, "1-before-pay", screenshot=False)
      self._hook_network(driver)
      mark_submitting()
      driver.execute_script("arguments[0].click()", pay)
      return self._read_outcome(driver, total)
    finally:
      driver.quit()

  @staticmethod
  def _fail(message: str):
    raise FormChanged(message)

  def _warm_up(self, driver) -> None:
    """Load a few ordinary pages before the checkout, so reCAPTCHA is not reading a browser that has visited
    nothing. A warm-up failure is never an outcome: any site may be slow or down, and none of this is the
    purchase. The Chrome profile persists between runs (appium:noReset), so this tops it up rather than
    building it from nothing every time."""
    if not self.warmup_urls:
      return
    deadline = time.monotonic() + self.warmup_budget
    notes = [f"budget {self.warmup_budget}s"]
    for url in self.warmup_urls:
      if time.monotonic() >= deadline:
        notes.append("budget spent; remaining sites skipped")
        break
      try:
        driver.get(url)
        time.sleep(1)  # a page a person opened would stay on screen at least this long
        notes.append(f"{url}: loaded")
        if "google.com" in url:
          notes.append(f"google session cookie present: {self._google_session(driver)}")
      except Exception as exc:
        notes.append(f"{url}: {type(exc).__name__}")
    self._write_diag("0-warmup", notes)

  @staticmethod
  def _google_session(driver) -> str:
    """Whether Chrome is carrying a signed-in Google session. Records cookie names only, never their values."""
    try:
      names = {str(cookie.get("name", "")) for cookie in driver.get_cookies()}
    except Exception:
      return "unknown"
    return "yes" if names & GOOGLE_SESSION_COOKIES else "no"

  @staticmethod
  def _recaptcha_challenged(driver) -> bool:
    """True only when a reCAPTCHA challenge is actually on screen, not for its always-present anchor iframe."""
    try:
      return bool(driver.execute_script(RECAPTCHA_CHALLENGE_JS))
    except Exception:
      return False  # a detection failure must not invent a challenge

  @staticmethod
  def _wait(predicate, what: str, timeout: int = 30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
      value = predicate()
      if value:
        return value
      time.sleep(0.5)
    raise FormChanged(f"timed out waiting for {what}")

  @staticmethod
  def _same(element_id: str, actual: str, expected: str) -> bool:
    if element_id == "parkerPhoneNumber":  # the site may reformat the number as it is typed
      return re.sub(r"\D", "", actual) == re.sub(r"\D", "", expected)
    return actual.strip().casefold() == expected.strip().casefold()

  def _set(self, driver, element_id: str, value: str) -> None:
    for _attempt in range(3):
      # Framework-controlled inputs ignore a plain `.value =`; the native setter plus events updates their state.
      driver.execute_script(
        "const e=document.getElementById(arguments[0]); e.focus();" +
        "Object.getOwnPropertyDescriptor(Object.getPrototypeOf(e),'value').set.call(e,arguments[1]);" +
        "for (const t of ['input','change','blur']) e.dispatchEvent(new Event(t,{bubbles:true}));",
        element_id, value)
      time.sleep(0.6)  # let the page's own scripts finish re-rendering the field
      actual = driver.execute_script("return document.getElementById(arguments[0]).value", element_id)
      if self._same(element_id, str(actual or ""), value):
        self._expected[element_id] = value
        return
    self._snapshot(driver, "0-could-not-set")
    raise FormChanged(f"could not set {element_id}")

  def _verify_fields(self, driver) -> None:
    """Re-read every field just before paying; the page may have cleared one after it was filled."""
    for element_id, expected in self._expected.items():
      actual = driver.execute_script("return (document.getElementById(arguments[0])||{}).value", element_id)
      if not self._same(element_id, str(actual or ""), expected):
        self._snapshot(driver, "0-field-changed")
        raise FormChanged(f"{element_id} changed before payment")

  def _fill_card_frame(self, driver) -> None:
    from selenium.webdriver.common.by import By

    frame = next((f for f in driver.find_elements(By.TAG_NAME, "iframe")
                  if "cardconnect.com" in (f.get_attribute("src") or "")), None)
    if frame is None:
      raise FormChanged("card iframe not found")
    driver.switch_to.frame(frame)
    try:
      month, year = split_expiry(self.expiration)
      values = {"number": self.card_number, "month": month, "year": year, "cvv": self.cvv}
      selectors = {"number": "#ccnumfield", "month": "#ccexpiryfieldmonth", "year": "#ccexpiryfieldyear", "cvv": "#cccvvfield"}
      log: list[str] = []
      for key, selector in selectors.items():
        found = driver.find_elements(By.CSS_SELECTOR, selector)
        if len(found) != 1:
          self._write_card_log(log + [f"{key}: expected one {selector}, found {len(found)}"])
          raise FormChanged(f"card {key} field was not found")
        found[0].click()
        found[0].send_keys(values[key])  # no clear(): the fields start empty and clearing may reset the tokenizer
        typed = self._card_digits(driver, selector)
        log.append(f"{key}: digits right after typing = {typed}")
        if typed != len(values[key]):
          problem_now = f"card {key} field did not accept the value"
          self._write_card_log(log)
          raise FormChanged(problem_now)
      time.sleep(1)
      problem = ""
      # The tokenizer masks the number to its last four digits and empties the CVV once it loses focus, so only
      # month, year and the number's last four digits can be read back; the CVV was verified while typing above.
      for key in ("number", "month", "year"):
        digits = self._card_digits(driver, selectors[key], raw=True)
        log.append(f"{key}: digits after 1s = {len(digits)}")
        wanted = values[key][-4:] if key == "number" else values[key]
        if not problem and (digits[-4:] if key == "number" else digits) != wanted:
          problem = f"card {key} field did not accept the value"
      self._write_card_log(log)
      if problem:
        raise FormChanged(problem)
    finally:
      driver.switch_to.default_content()

  @staticmethod
  def _card_digits(driver, selector: str, *, raw: bool = False):
    """The digits currently in a card input, read from the live DOM property (a count unless raw=True)."""
    value = driver.execute_script("const e=document.querySelector(arguments[0]); return e ? e.value : ''", selector) or ""
    digits = re.sub(r"\D", "", value)
    return digits if raw else len(digits)

  def _write_diag(self, label: str, lines: list[str]) -> None:
    from pathlib import Path

    try:
      directory = Path(os.getenv("PARKING_DIAG_DIR", "/var/lib/parking-demo/diag"))
      directory.mkdir(mode=0o700, parents=True, exist_ok=True)
      (directory / f"{self._stamp}-{label}.txt").write_text("\n".join(lines) + "\n")
    except Exception:
      pass  # diagnostics must never change the recorded outcome

  def _write_card_log(self, lines: list[str]) -> None:
    """Record how many digits each card input held (never the digits) so a mismatch can be diagnosed."""
    self._write_diag("0-card-fields", lines)

  @staticmethod
  def _classify_card_fields(fields) -> dict:
    """Map the tokenizer's inputs to number/month/year/cvv by their hints, else by the usual on-screen order."""
    slots: dict = {}
    for field in fields:
      hint = " ".join((field.get_attribute(a) or "") for a in ("id", "name", "placeholder", "aria-label")).lower()
      placeholder = (field.get_attribute("placeholder") or "").strip().lower()
      if "cvv" in hint or "cvc" in hint or "security" in hint:
        slots.setdefault("cvv", field)
      elif placeholder == "mm" or "month" in hint:
        slots.setdefault("month", field)
      elif placeholder in ("yyyy", "yy") or "year" in hint:
        slots.setdefault("year", field)
      elif "card" in hint or "number" in hint or "ccnum" in hint:
        slots.setdefault("number", field)
    if len(slots) < 4 and len(fields) == 4:
      slots = dict(zip(("number", "month", "year", "cvv"), fields, strict=True))
    if set(slots) != {"number", "month", "year", "cvv"}:
      raise FormChanged("could not identify the card number, month, year and CVV fields")
    return slots

  def _read_outcome(self, driver, total_minor: int) -> dict[str, object]:
    from selenium.webdriver.common.by import By

    started = time.monotonic()
    deadline = started + 45
    pending_snapshots = [(3, "2-after-pay-3s"), (10, "3-after-pay-10s")]
    captcha_seen = False
    while time.monotonic() < deadline:
      if pending_snapshots and time.monotonic() - started >= pending_snapshots[0][0]:
        self._snapshot(driver, pending_snapshots.pop(0)[1])
      # PAY has already been clicked, so a challenge here cannot be reported as "nothing was purchased";
      # record it and let the usual unknown-outcome path run.
      if not captcha_seen and self._recaptcha_challenged(driver):
        captcha_seen = True
        self._snapshot(driver, "5-recaptcha-after-pay")
      text = driver.find_element(By.TAG_NAME, "body").text
      if is_decline(text):
        raise PaymentDeclined("card was declined")
      if any(marker in text.lower() for marker in RESERVATION_MARKERS):
        self._snapshot(driver, "5-reservation-rejected")
        self._dump_network(driver)
        raise ReservationRejected("LAZ rejected the reservation")
      if "PAY $" not in text and "receipt" in text.lower():
        return {"demo": False, "message": "Parking purchased.", "total_minor": total_minor,
                "completed_unix_ms": time.time_ns() // 1_000_000}
      time.sleep(1)
    self._snapshot(driver, "4-final-unrecognised")
    if captcha_seen:
      raise SubmissionUnknown("payment outcome was not observed; reCAPTCHA challenged after PAY")
    raise SubmissionUnknown("payment outcome was not observed")

  _stamp = "run"

  @staticmethod
  def _hook_network(driver) -> None:
    """Record the page's own fetch/XHR calls (URL, status, short response) so a rejected reservation can be explained."""
    try:
      driver.execute_script("""
        if (window.__net) return; window.__net = [];
        const keep = (m, u, s, t) => window.__net.push([m, String(u).slice(0, 200), s, String(t).slice(0, 400)]);
        const f = window.fetch;
        window.fetch = function(u, o) { const m = (o && o.method) || 'GET';
          return f.apply(this, arguments).then(r => { r.clone().text().then(t => keep(m, r.url || u, r.status, t)); return r; },
                                                 e => { keep(m, u, 0, String(e)); throw e; }); };
        const open = XMLHttpRequest.prototype.open;
        XMLHttpRequest.prototype.open = function(m, u) {
          this.addEventListener('loadend', () => keep(m, u, this.status, this.responseText));
          return open.apply(this, arguments);
        };
      """)
    except Exception:
      pass  # diagnostics must never change the recorded outcome

  def _dump_network(self, driver) -> None:
    from pathlib import Path

    try:
      rows = driver.execute_script("return window.__net || []")
      directory = Path(os.getenv("PARKING_DIAG_DIR", "/var/lib/parking-demo/diag"))
      directory.mkdir(mode=0o700, parents=True, exist_ok=True)
      lines = [re.sub(r"\d{12,19}", "[number]", f"{m} {u} -> {s}: {t}") for m, u, s, t in rows]
      (directory / f"{self._stamp}-6-network.txt").write_text("\n".join(lines) + "\n")
    except Exception:
      pass  # diagnostics must never change the recorded outcome

  def _snapshot(self, driver, label: str, *, screenshot: bool = True) -> None:
    """Record page text, visible validation messages and (optionally) a screenshot. Long digit runs are scrubbed."""
    from pathlib import Path

    from selenium.webdriver.common.by import By

    try:
      directory = Path(os.getenv("PARKING_DIAG_DIR", "/var/lib/parking-demo/diag"))
      directory.mkdir(mode=0o700, parents=True, exist_ok=True)
      text = re.sub(r"\d{12,19}", "[number]", driver.find_element(By.TAG_NAME, "body").text)
      problems = driver.execute_script(
        "return [...document.querySelectorAll('.is-invalid,.invalid-feedback,.error,[role=alert],.alert')]" +
        ".filter(e=>e.offsetWidth).map(e=>((e.id||e.className)+': '+(e.innerText||'').trim()).slice(0,120))") or []
      body = f"{driver.current_url}\n\nVISIBLE PROBLEMS: {problems}\n\n{text}\n"
      (directory / f"{self._stamp}-{label}.txt").write_text(body)
      if screenshot:
        # Never capture the card fields: hide the payment iframe for the shot.
        driver.execute_script("document.querySelectorAll('iframe').forEach(f=>f.style.visibility='hidden')")
        try:
          driver.get_screenshot_as_file(str(directory / f"{self._stamp}-{label}.png"))
        finally:
          driver.execute_script("document.querySelectorAll('iframe').forEach(f=>f.style.visibility='')")
    except Exception:
      pass  # diagnostics must never change the recorded outcome


def urlparse_host(url: str) -> str:
  from urllib.parse import urlparse
  return urlparse(url).hostname or ""
