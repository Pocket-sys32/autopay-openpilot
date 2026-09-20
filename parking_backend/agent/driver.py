"""The one place an agent browser session is built and driven.

This deliberately reuses the capability set the LAZ work arrived at, because those choices were paid for:
attaching to a running Chrome keeps the signed-in Google session that reCAPTCHA scores on, and the value
setter below is what makes React-style fields actually accept input.
"""
from __future__ import annotations

import os
import re
import time
from urllib.parse import urlsplit

from parking_backend.agent.dom import capture, harvest, resolve
from parking_backend.agent.secrets import SecretVault
from parking_backend.agent.types import Observation, StaleNode
from parking_backend.appium_adapter import CHROMEDRIVER_PATH, FormChanged


# Long enough to outlast a driver deciding at the confirmation prompt; the worker also sends keepalives.
NEW_COMMAND_TIMEOUT_S = 600
PAGE_LOAD_TIMEOUT_S = 30
DEFAULT_WARMUP_URLS = ("https://www.google.com/", "https://en.wikipedia.org/")
SECURITY_VERIFICATION_ATTEMPTS = 12
OBSERVE_ATTEMPTS = 3
TEXT_ENTRY_ATTEMPTS = 3
TEXT_ENTRY_SETTLE_S = 0.6
BY_CSS_SELECTOR = "css selector"
BY_TAG_NAME = "tag name"
_AUTOCOMPLETE_SLOTS = {
  "cc-number": "card_number", "cc-csc": "card_cvv", "cc-exp": "card_expiry",
  "cc-exp-month": "card_expiry_month", "cc-exp-year": "card_expiry_year", "postal-code": "card_zip",
}
LAZ_CHECKOUT_HOST = "go.lazparking.com"
CARDCONNECT_DOMAIN = "cardconnect.com"
LAZ_CARD_SLOTS = ("card_number", "card_expiry_month", "card_expiry_year", "card_cvv")
LAZ_RETAINED_CARD_SLOTS = ("card_number", "card_expiry_month", "card_expiry_year")


def _set_value_js() -> str:
  """Set a field the way a person would, so frameworks notice.

  A plain value assignment is invisible to React and friends; the native setter plus the input/change pair
  is what the LAZ checkout actually responded to."""
  return """
  const e = arguments[0], v = arguments[1];
  const proto = Object.getPrototypeOf(e);
  const setter = Object.getOwnPropertyDescriptor(proto, 'value');
  e.focus();
  if (setter && setter.set) { setter.set.call(e, v); } else { e.value = v; }
  for (const t of ['input', 'change', 'blur']) e.dispatchEvent(new Event(t, {bubbles: true}));
  return e.value;
  """


class DriverSession:
  """A real Chrome on the emulator, presented as the Browser the agent loop expects."""

  def __init__(self, appium_url: str, *, vault: SecretVault | None = None, attach: bool | None = None,
               warmup_urls: tuple[str, ...] | None = None, warmup_budget_s: int = 30):
    self.appium_url = appium_url
    self.vault = vault
    self.attach = os.getenv("PARKING_AGENT_ATTACH_CHROME", "1") == "1" if attach is None else attach
    configured = os.getenv("PARKING_AGENT_WARMUP_URLS")
    if warmup_urls is not None:
      self.warmup_urls = warmup_urls
    else:
      self.warmup_urls = (tuple(u.strip() for u in configured.split(",") if u.strip())
                          if configured is not None else DEFAULT_WARMUP_URLS)
    self.warmup_budget_s = warmup_budget_s
    self._driver = None
    self._warmed = False

  # -- lifecycle -----------------------------------------------------------------------------------

  @property
  def driver(self):
    if self._driver is None:
      self._driver = self._connect()
    return self._driver

  def _connect(self):
    from appium import webdriver
    from appium.options.android import UiAutomator2Options

    options = UiAutomator2Options()
    options.platform_name = "Android"
    options.browser_name = "Chrome"
    options.automation_name = "UiAutomator2"
    options.set_capability("appium:newCommandTimeout", NEW_COMMAND_TIMEOUT_S)
    options.set_capability("appium:noReset", True)
    options.set_capability("appium:chromedriverExecutable", CHROMEDRIVER_PATH)
    chrome_options: dict[str, object] = {"args": ["--disable-blink-features=AutomationControlled"]}
    if self.attach:
      # Left alone, chromedriver relaunches Chrome with a cleared profile, signing the browser out before
      # the first page loads -- and that session is exactly what reCAPTCHA reads.
      chrome_options["androidUseRunningApp"] = True
      chrome_options["androidPackage"] = "com.android.chrome"
    options.set_capability("appium:chromeOptions", chrome_options)
    try:
      driver = webdriver.Remote(self.appium_url, options=options)
    except Exception as exc:
      if not self.attach:
        raise
      # No fallback to a normal launch: it would succeed while silently signing the browser out.
      message = "could not attach to a running Chrome; open it on the device and check it is signed in"
      raise FormChanged(message) from exc
    driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT_S)
    return driver

  def _warm_up(self) -> None:
    """Visit a couple of ordinary sites first. A profile that has just been opened cold scores badly."""
    if self._warmed or not self.warmup_urls:
      return
    self._warmed = True
    deadline = time.monotonic() + self.warmup_budget_s
    for url in self.warmup_urls:
      if time.monotonic() >= deadline:
        break
      try:
        self.driver.get(url)
      except Exception:
        continue  # warming is not the purchase; a slow or failed site is not a reason to stop

  def quit(self) -> None:
    driver, self._driver = self._driver, None
    if driver is not None:
      try:
        driver.quit()
      except Exception:
        pass

  def keepalive(self) -> None:
    """Cheap round trip so a session parked at a checkout does not lapse while a person decides."""
    _ = self.driver.current_url

  # -- the Browser surface the loop uses -----------------------------------------------------------

  def observe(self, step: int) -> Observation:
    # Chrome can replace its execution context in the narrow interval immediately after a navigation click.
    # Re-read that new document; a persistent script error still escapes after this short fixed bound.
    for attempt in range(OBSERVE_ATTEMPTS):
      try:
        return harvest(self.driver, step, vault=self.vault)
      except Exception as exc:
        # Selenium is an optional runtime dependency in local unit tests, so identify its precise exception
        # without importing the package merely to exercise this retry boundary.
        if type(exc).__name__ != "JavascriptException":
          raise
        if attempt + 1 == OBSERVE_ATTEMPTS:
          raise
        time.sleep(0.5)
    raise AssertionError("unreachable")

  def open_url(self, url: str) -> None:
    self._warm_up()
    self.driver.get(url)
    _wait_for_security_verification(self.driver)

  def tap(self, nid: str) -> None:
    element = resolve(self.driver, nid)
    # Scroll only; let WebDriver perform the actual click so normal hit-testing, focus and event ordering apply.
    # If an overlay intercepts it, fail and re-observe rather than bypassing what the user would see.
    self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
    element.click()

  def type_text(self, nid: str, text: str) -> None:
    # Controlled inputs can briefly echo a write and then be restored by their framework on the next render.
    # Settle and re-read the live property, retrying only this bounded idempotent write.
    for _attempt in range(TEXT_ENTRY_ATTEMPTS):
      element = resolve(self.driver, nid)
      self.driver.execute_script(_set_value_js(), element, text)
      time.sleep(TEXT_ENTRY_SETTLE_S)
      actual = str(element.get_attribute("value") or "")
      input_type = str(element.get_attribute("type") or "")
      if _text_was_accepted(input_type, text, actual):
        return
    raise FormChanged(f"the field behind {nid} did not retain the value")

  def fill_secret(self, slot: str, value: str) -> None:
    """Resolve a payment field structurally, including cross-origin tokenizer frames.

    The model supplies only the slot name. It never receives or addresses the underlying element."""
    driver = self.driver
    try:
      location = _find_secret_field(driver, slot)
      if location is None:
        raise FormChanged(f"exactly one visible {slot} field was not found")
      frame_index, element_index = location
      _enter_frame(driver, frame_index)
      elements = [element for element in driver.find_elements(BY_CSS_SELECTOR, "input") if element.is_displayed()]
      if element_index >= len(elements) or classify_card_field(_field_attributes(elements[element_index])) != slot:
        raise StaleNode(f"the {slot} field changed before it could be filled")
      element = elements[element_index]
      element.click()
      element.send_keys(value)
      if not _secret_was_accepted(slot, value, _secret_value(self.driver, element)):
        raise FormChanged(f"the {slot} field did not accept its configured value")
    finally:
      driver.switch_to.default_content()

  def prepare_laz_payment(self, host: str) -> tuple[str, ...]:
    """Fill LAZ's known CardConnect fields without asking the model to infer their shape.

    Card inputs are deliberately absent from observations, so a model can know that payment fields exist
    but cannot safely choose between one combined expiry box and LAZ's separate month/year boxes.  Keep the
    provider-specific choice here, next to the structural field resolver and the secret vault.
    """
    normalized = host.lower().strip(".")
    if normalized != LAZ_CHECKOUT_HOST:
      return ()
    current_host = (urlsplit(str(self.driver.current_url or "")).hostname or "").lower().strip(".")
    if current_host != LAZ_CHECKOUT_HOST:
      raise FormChanged("the LAZ checkout host changed before its payment fields were filled")
    if self.vault is None:
      raise FormChanged("no payment details are configured on this backend")

    # Discover the single CardConnect frame and all four fields in one pass before writing any secret. This
    # is both stricter and much faster than rescanning every unrelated frame once per slot on mobile Appium.
    try:
      fields = _laz_card_fields(self.driver)
      for slot in LAZ_CARD_SLOTS:
        element = fields[slot]
        value = self.vault.get(slot)
        element.click()
        element.send_keys(value)
        if not _secret_was_accepted(slot, value, _secret_value(self.driver, element)):
          raise FormChanged(f"the {slot} field did not accept its configured value")
    finally:
      self.driver.switch_to.default_content()
    self.verify_laz_payment(host, LAZ_CARD_SLOTS)
    return LAZ_CARD_SLOTS

  def verify_laz_payment(self, host: str, slots: tuple[str, ...]) -> None:
    """Re-read the stable CardConnect fields immediately before PAY, without exposing their values.

    CardConnect may consume/blank CVV after its input loses focus, so its configured length is checked at the
    moment it is typed by ``fill_secret``.  The number (possibly masked), month and year remain readable and
    are checked again here after every field has been filled.
    """
    if host.lower().strip(".") != LAZ_CHECKOUT_HOST or tuple(slots) != LAZ_CARD_SLOTS:
      raise FormChanged("the prepared LAZ payment fields no longer match the expected form")
    current_host = (urlsplit(str(self.driver.current_url or "")).hostname or "").lower().strip(".")
    if current_host != LAZ_CHECKOUT_HOST:
      raise FormChanged("the LAZ checkout host changed before payment")
    if self.vault is None:
      raise FormChanged("no payment details are configured on this backend")
    try:
      fields = _laz_card_fields(self.driver)
      for slot in LAZ_RETAINED_CARD_SLOTS:
        actual = _secret_value(self.driver, fields[slot])
        if not _secret_was_accepted(slot, self.vault.get(slot), actual):
          raise FormChanged(f"the {slot} field changed before payment")
    finally:
      self.driver.switch_to.default_content()

  def select(self, nid: str, option_text: str) -> None:
    from selenium.webdriver.support.ui import Select
    element = resolve(self.driver, nid)
    if element.tag_name.lower() == "select":
      Select(element).select_by_visible_text(option_text)
      self.driver.execute_script("arguments[0].dispatchEvent(new Event('change', {bubbles: true}));", element)
      return
    # A custom dropdown: open it, then click the option that matches.
    self.tap(nid)
    self.driver.execute_script("""
      const wanted = arguments[0].trim().toLowerCase();
      const candidates = document.querySelectorAll("[role='option'], li, option");
      for (const c of candidates) {
        if ((c.innerText || c.textContent || '').trim().toLowerCase() === wanted) { c.click(); return true; }
      }
      return false;
    """, option_text)

  def scroll(self, direction: str, nid: str = "") -> None:
    if nid:
      resolve(self.driver, nid)
      self.driver.execute_script(
        "document.querySelector(arguments[0]).scrollIntoView({block: 'center'});", f'[data-pa-nid="{nid}"]')
      return
    self.driver.execute_script("window.scrollBy(0, arguments[0] * window.innerHeight * 0.8);",
                               1 if direction == "down" else -1)

  def back(self) -> None:
    self.driver.back()

  def wait(self, seconds: int) -> None:
    time.sleep(min(seconds, 5))

  def screenshot(self) -> bytes | None:
    return capture(self.driver)


def _wait_for_security_verification(driver) -> None:
  """Let a provider's own transient bot check finish; never click or attempt to solve a challenge."""
  for reload_left in (1, 0):
    for _ in range(SECURITY_VERIFICATION_ATTEMPTS):
      try:
        if "just a moment" not in str(driver.title).lower():
          return
      except Exception:
        return  # the agent will observe and classify whatever page is actually available
      time.sleep(1)
    if reload_left:
      try:
        driver.refresh()
      except Exception:
        return


def _text_was_accepted(input_type: str, expected: str, actual: str) -> bool:
  """Compare what survived the page's own render without exposing either value to diagnostics."""
  if input_type.strip().lower() == "tel":
    return re.sub(r"\D", "", actual) == re.sub(r"\D", "", expected)
  return actual.strip().casefold() == expected.strip().casefold()


def classify_card_field(attributes: dict[str, str]) -> str | None:
  """Classify one input from metadata only; values are never inspected or logged."""
  autocomplete = attributes.get("autocomplete", "").strip().lower()
  if autocomplete in _AUTOCOMPLETE_SLOTS:
    return _AUTOCOMPLETE_SLOTS[autocomplete]
  text = " ".join(attributes.get(key, "") for key in ("name", "id", "placeholder", "aria-label")).lower()
  compact = re.sub(r"[^a-z0-9]", "", text)
  if any(token in compact for token in ("cardholder", "nameoncard", "cardname")):
    return None
  if any(token in compact for token in ("securitycode", "cardverification", "cvv", "cvc", "cvn", "csc")):
    return "card_cvv"
  if any(token in compact for token in ("expirymonth", "expirationmonth", "expiryfieldmonth", "expmonth", "ccmonth")):
    return "card_expiry_month"
  if any(token in compact for token in ("expiryyear", "expirationyear", "expiryfieldyear", "expyear", "ccyear")):
    return "card_expiry_year"
  if any(token in compact for token in ("expirydate", "expirationdate", "cardexpiry", "cardexpiration", "ccexp")):
    return "card_expiry"
  if any(token in compact for token in ("billingzip", "billingpostal", "cardzip", "cardpostal")):
    return "card_zip"
  if any(token in compact for token in ("cardnumber", "ccnumber", "ccnum", "panfield", "accountnumber")):
    return "card_number"
  return None


def _field_attributes(element) -> dict[str, str]:
  return {key: str(element.get_attribute(key) or "")
          for key in ("autocomplete", "name", "id", "placeholder", "aria-label")}


def _enter_frame(driver, frame_index: int | None) -> None:
  driver.switch_to.default_content()
  if frame_index is not None:
    frames = driver.find_elements(BY_TAG_NAME, "iframe")
    if frame_index >= len(frames):
      raise StaleNode("the payment frame changed before it could be filled")
    driver.switch_to.frame(frames[frame_index])


def _find_secret_field(driver, slot: str) -> tuple[int | None, int] | None:
  driver.switch_to.default_content()
  frames = driver.find_elements(BY_TAG_NAME, "iframe")
  matches: list[tuple[int | None, int]] = []
  for frame_index in (None, *range(len(frames))):
    try:
      _enter_frame(driver, frame_index)
      visible = [element for element in driver.find_elements(BY_CSS_SELECTOR, "input") if element.is_displayed()]
      matches.extend((frame_index, index) for index, element in enumerate(visible)
                     if classify_card_field(_field_attributes(element)) == slot)
    except Exception:
      continue  # an unrelated cross-origin frame may not expose a document through WebDriver
  driver.switch_to.default_content()
  return matches[0] if len(matches) == 1 else None


def _laz_card_fields(driver) -> dict[str, object]:
  """Return LAZ's four uniquely classified inputs from one exact CardConnect iframe."""
  driver.switch_to.default_content()
  matched_frames = []
  for frame in driver.find_elements(BY_TAG_NAME, "iframe"):
    try:
      parsed = urlsplit(str(frame.get_attribute("src") or ""))
      host = (parsed.hostname or "").lower().strip(".")
      if (parsed.scheme.lower() == "https" and
          (host == CARDCONNECT_DOMAIN or host.endswith(f".{CARDCONNECT_DOMAIN}")) and
          parsed.username is None and parsed.password is None):
        matched_frames.append(frame)
    except Exception:
      continue
  if len(matched_frames) != 1:
    raise FormChanged("exactly one CardConnect payment frame was not found")
  driver.switch_to.frame(matched_frames[0])
  fields: dict[str, object] = {}
  for element in driver.find_elements(BY_CSS_SELECTOR, "input"):
    if not element.is_displayed():
      continue
    slot = classify_card_field(_field_attributes(element))
    if slot not in LAZ_CARD_SLOTS:
      continue
    if slot in fields:
      raise FormChanged(f"more than one visible {slot} field was found")
    fields[slot] = element
  missing = [slot for slot in LAZ_CARD_SLOTS if slot not in fields]
  if missing:
    raise FormChanged(f"exactly one visible {missing[0]} field was not found")
  return fields


def _secret_value(driver, element) -> str:
  """Read the live tokenizer property; ChromeDriver can expose an empty HTML attribute for a filled field."""
  return str(driver.execute_script("return arguments[0].value || '';", element) or "")


def _secret_was_accepted(slot: str, expected: str, actual: str) -> bool:
  expected_digits = re.sub(r"\D", "", expected)
  actual_digits = re.sub(r"\D", "", actual)
  if slot == "card_number":
    return len(actual_digits) == len(expected_digits) or actual_digits[-4:] == expected_digits[-4:]
  if slot == "card_cvv":
    return len(actual_digits) == len(expected_digits)
  if slot in ("card_expiry", "card_expiry_month", "card_expiry_year"):
    return actual_digits.endswith(expected_digits[-len(actual_digits):]) and bool(actual_digits)
  return actual.strip().replace(" ", "").upper() == expected.strip().replace(" ", "").upper()
