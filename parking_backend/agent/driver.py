"""The one place an agent browser session is built and driven.

This deliberately reuses the capability set the LAZ work arrived at, because those choices were paid for:
attaching to a running Chrome keeps the signed-in Google session that reCAPTCHA scores on, and the value
setter below is what makes React-style fields actually accept input.
"""
from __future__ import annotations

import os
import time

from parking_backend.agent.dom import capture, harvest, resolve
from parking_backend.agent.secrets import SecretVault
from parking_backend.agent.types import Observation
from parking_backend.appium_adapter import CHROMEDRIVER_PATH, FormChanged


# Long enough to outlast a driver deciding at the confirmation prompt; the worker also sends keepalives.
NEW_COMMAND_TIMEOUT_S = 600
PAGE_LOAD_TIMEOUT_S = 30
DEFAULT_WARMUP_URLS = ("https://www.google.com/", "https://en.wikipedia.org/")


def _set_value_js() -> str:
  """Set a field the way a person would, so frameworks notice.

  A plain value assignment is invisible to React and friends; the native setter plus the input/change pair
  is what the LAZ checkout actually responded to."""
  return """
  const e = arguments[0], v = arguments[1];
  const proto = e instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, 'value');
  e.focus();
  if (setter && setter.set) { setter.set.call(e, v); } else { e.value = v; }
  e.dispatchEvent(new Event('input', {bubbles: true}));
  e.dispatchEvent(new Event('change', {bubbles: true}));
  e.dispatchEvent(new Event('blur', {bubbles: true}));
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
    return harvest(self.driver, step, vault=self.vault)

  def open_url(self, url: str) -> None:
    self._warm_up()
    self.driver.get(url)

  def tap(self, nid: str) -> None:
    element = resolve(self.driver, nid)
    # A JS click lands on elements an overlay would otherwise intercept.
    self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'}); arguments[0].click();", element)

  def type_text(self, nid: str, text: str) -> None:
    element = resolve(self.driver, nid)
    written = self.driver.execute_script(_set_value_js(), element, text)
    if written != text:
      raise FormChanged(f"the field behind {nid} did not accept the value")

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
