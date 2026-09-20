"""No-payment check of the browser state the LAZ entry and checkout pages would see.

It never calls FlareSolverr, fills the checkout form, clicks PAY or buys anything. It is still a live
anti-abuse probe, so run it once for an explicitly authorised diagnosis rather than polling or retrying it.
Run it on the VM, against the local Appium:

    PYTHONPATH=/tmp/proberoot PARKING_DIAG_DIR=/tmp/probe-diag \\
      /opt/parking-demo/.venv/bin/python /tmp/proberoot/parking_backend/deploy/probe_browser.py
"""
from __future__ import annotations

import os
import time

from parking_backend.appium_adapter import CHROMEDRIVER_PATH
from parking_backend.laz_adapter import ENTRY_URL, RECAPTCHA_CHALLENGE_JS, LazAdapter, LazProfile


def main() -> None:
  from appium import webdriver
  from appium.options.android import UiAutomator2Options

  # Only the warm-up and the read-only probes are used; no card or payer details are needed or read.
  probe = LazAdapter(os.getenv("PARKING_APPIUM_URL", "http://127.0.0.1:4723"),
                     card_number="", cvv="", expiration="12/30",
                     profile=LazProfile("probe@example.com", "5555555555", "1 Main St", "00000"))

  options = UiAutomator2Options()
  options.platform_name = "Android"
  options.browser_name = "Chrome"
  options.automation_name = "UiAutomator2"
  options.set_capability("appium:newCommandTimeout", 150)
  options.set_capability("appium:noReset", True)
  options.set_capability("appium:chromedriverExecutable", CHROMEDRIVER_PATH)
  configured_attach = os.getenv("PARKING_PROBE_ATTACH")
  attach = probe.attach_to_chrome if configured_attach is None else configured_attach == "1"
  chrome_options: dict[str, object] = {}
  if attach:
    # chromedriver otherwise relaunches Chrome with a cleared data directory, which throws away the signed-in
    # session before the page is ever loaded. Attaching to the running app keeps the profile.
    chrome_options["androidUseRunningApp"] = True
    chrome_options["androidPackage"] = "com.android.chrome"
  options.set_capability("appium:chromeOptions", chrome_options)

  driver = webdriver.Remote(probe.appium_url, options=options)
  driver.set_page_load_timeout(30)
  try:
    probe._stamp = time.strftime("%Y%m%dT%H%M%S")
    print(f"browser mode                  : {'attached persistent profile' if attach else 'new browser session'}")
    print("FlareSolverr exercised        : no")
    probe._warm_up(driver)

    print(f"google navigation             : {_navigate(driver, 'https://www.google.com/')}")
    time.sleep(1)
    print(f"google session cookie present : {probe._google_session(driver)}")
    print(f"cookie names visible here     : {_cookie_names(driver)}")
    print(f"navigator.webdriver           : {driver.execute_script('return navigator.webdriver')}")
    print(f"user agent                    : {driver.execute_script('return navigator.userAgent')}")
    print(f"signed in per google          : {_signed_in(driver)}")  # navigates away, so read cookies first

    print(f"LAZ navigation                : {_navigate(driver, ENTRY_URL)}")
    time.sleep(3)
    go_button = _wait_for_go(driver)
    print(f"cloudflare passed (GO button) : {'yes' if go_button else 'no'}")
    print(f"recaptcha on the entry page   : {_recaptcha_frames(driver)}")
    print(f"recaptcha challenge visible   : {bool(driver.execute_script(RECAPTCHA_CHALLENGE_JS))}")
    print(f"entry url                     : {driver.current_url}")

    # Opt-in: walk to the checkout form, which is where the challenge actually appears. This fills nothing
    # and never clicks PAY, so no reservation is made and no card is entered.
    if go_button and os.getenv("PARKING_PROBE_CHECKOUT") == "1":
      _to_checkout(driver)
      print(f"checkout form reached         : {_has_checkout_form(driver)}")
      print(f"recaptcha at the checkout     : {_recaptcha_frames(driver)}")
      print(f"challenge at the checkout     : {bool(driver.execute_script(RECAPTCHA_CHALLENGE_JS))}")
      print(f"checkout url                  : {driver.current_url}")
  finally:
    driver.quit()


def _signed_in(driver) -> str:
  """Ask Google directly: a signed-out browser is bounced from the account page to a sign-in screen. Reading
  the homepage's markup instead looks conclusive and is not — those pages mention "Google Account" either way."""
  try:
    outcome = _navigate(driver, "https://myaccount.google.com/")
    time.sleep(2)
    url = driver.current_url
  except Exception:
    return "unknown"
  # Report where it actually landed: "did not end up on a sign-in page" is not the same as "reached the
  # account page", and a navigation that quietly failed would otherwise read as signed in.
  if "myaccount.google.com" in url:
    return f"yes, reached {url[:70]} ({outcome})"
  return f"no, bounced to {url[:70]} ({outcome})"


def _navigate(driver, url: str) -> str:
  """A renderer timeout is diagnostic data, not a reason for this no-payment probe to crash."""
  try:
    driver.get(url)
  except Exception as exc:
    return type(exc).__name__
  return "loaded"


def _cookie_names(driver) -> str:
  """Cookie names only, never values. If this omits the HttpOnly session cookies the page clearly sent, the
  name check above is blind rather than the browser being signed out."""
  try:
    names = sorted({str(c.get("name", "")) for c in driver.get_cookies()})
  except Exception as exc:
    return f"unreadable ({type(exc).__name__})"
  return f"{len(names)}: " + ", ".join(names[:12]) if names else "none"


def _to_checkout(driver) -> None:
  """Entry page -> GO -> NEXT -> checkout form. Reads and clicks navigation only; fills nothing, pays nothing."""
  driver.execute_script("document.getElementById('buyNowSearch').click()")
  for _ in range(30):
    button = driver.execute_script(
      "return [...document.querySelectorAll('button')].find(b=>b.innerText.trim()==='NEXT'&&b.offsetWidth)||null")
    if button:
      driver.execute_script("arguments[0].click()", button)
      break
    time.sleep(1)
  for _ in range(30):
    if _has_checkout_form(driver):
      break
    time.sleep(1)
  time.sleep(3)  # let the checkout finish rendering before reading its frames


def _has_checkout_form(driver) -> bool:
  try:
    return bool(driver.execute_script("return !!document.getElementById('parkerLicensePlate')"))
  except Exception:
    return False


def _wait_for_go(driver) -> bool:
  for reload_left in (1, 0):
    for _ in range(30):
      try:
        if driver.execute_script("return !!document.getElementById('buyNowSearch')"):
          return True
      except Exception:
        pass
      time.sleep(1)
    if reload_left:
      _navigate(driver, ENTRY_URL)
  return False


def _recaptcha_frames(driver) -> str:
  try:
    frames = driver.execute_script(
      "return [...document.querySelectorAll('iframe')].map(f=>f.src||'').filter(s=>s.includes('recaptcha'))") or []
  except Exception:
    return "unknown"
  if not frames:
    return "none"
  return ", ".join("anchor" if "anchor" in s else "bframe" if "bframe" in s else s[:60] for s in frames)


if __name__ == "__main__":
  main()
