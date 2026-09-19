from __future__ import annotations

from collections.abc import Callable
import os
import time


FORM_URL = "https://docs.google.com/forms/d/e/1FAIpQLSfsn3xRdGLVcJXyoSBccSGiPYMi_fCqja-0Iay87If5Ncmu_Q/viewform"
FORM_TITLE = "Comma Hack 7 Parking"
CONFIRMATION_TEXT = "Your response has been recorded."
FORM_ID = "1FAIpQLSfsn3xRdGLVcJXyoSBccSGiPYMi_fCqja-0Iay87If5Ncmu_Q"
CHROMEDRIVER_PATH = os.getenv("PARKING_CHROMEDRIVER_PATH", "/opt/android-sdk/chromedriver/chromedriver")


class FormChanged(RuntimeError):
  pass


class SubmissionUnknown(RuntimeError):
  pass


class AndroidFormAdapter:
  """Appium adapter for the one allowlisted Google Form."""

  def __init__(self, appium_url: str, *, card_number: str, cvv: str, expiration: str, zip_code: str):
    self.appium_url = appium_url
    self.card_number = card_number
    self.cvv = cvv
    self.expiration = expiration
    self.zip_code = zip_code

  def validate_location(self, location_id: str) -> bool:
    return location_id == FORM_ID

  def get_quote(self, *, location_id: str, plate: str, duration_seconds: int) -> dict[str, object]:
    if not self.validate_location(location_id) or duration_seconds not in (3600, 7200):
      raise ValueError("unsupported demo location or duration")
    return {"location_id": location_id, "plate": plate, "duration_seconds": duration_seconds,
            "total_minor": 0, "currency": "USD", "demo": True}

  def lookup_session(self, attempt_id: str) -> dict[str, object] | None:
    # Google Forms has no provider-side lookup or idempotency key.
    return None

  def submit(self, request: dict[str, object], *, mark_submitting: Callable[[], None]) -> dict[str, object]:
    from appium import webdriver
    from appium.options.android import UiAutomator2Options
    from selenium.common.exceptions import TimeoutException
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    options = UiAutomator2Options()
    options.platform_name = "Android"
    options.browser_name = "Chrome"
    options.automation_name = "UiAutomator2"
    options.set_capability("appium:newCommandTimeout", 150)
    options.set_capability("appium:noReset", True)
    options.set_capability("appium:chromedriverExecutable", CHROMEDRIVER_PATH)
    driver = webdriver.Remote(self.appium_url, options=options)
    driver.set_page_load_timeout(30)
    try:
      driver.get(FORM_URL)
      wait = WebDriverWait(driver, 20)
      wait.until(EC.title_contains(FORM_TITLE))
      page_text = driver.find_element(By.TAG_NAME, "body").text
      required_labels = ("Parking Duration", "License Plate", "Credit Card #", "Credit Card CVV", "Credit Card Expiration Date", "Zip Code")
      if any(label not in page_text for label in required_labels):
        raise FormChanged("expected form fields are missing")
      if "Sign in to Google" not in page_text and "Never submit passwords through Google Forms" not in page_text:
        raise FormChanged("unexpected form identity")

      duration_label = "1 Hour" if request["duration_seconds"] == 3600 else "2 Hours"
      duration = wait.until(EC.element_to_be_clickable((By.XPATH, f"//*[@role='radio' and @data-value='{duration_label}']")))
      duration.click()

      textboxes = driver.find_elements(By.CSS_SELECTOR, "input[type='text'], textarea")
      if len(textboxes) != 5:
        raise FormChanged(f"expected five text fields, found {len(textboxes)}")
      values = [str(request["plate"]), self.card_number, self.cvv, self.expiration, self.zip_code]
      for textbox, value in zip(textboxes, values, strict=True):
        textbox.clear()
        textbox.send_keys(value)
      if duration.get_attribute("aria-checked") != "true":
        raise FormChanged("duration selection did not stick")
      for textbox, expected in zip(textboxes, values, strict=True):
        if driver.execute_script("return arguments[0].value;", textbox) != expected:
          raise FormChanged("form field verification failed")

      submit = wait.until(EC.element_to_be_clickable(
        (By.XPATH, "//*[@role='button' and .//*[normalize-space()='Submit']]"),
      ))
      mark_submitting()
      submit.click()
      try:
        wait.until(EC.text_to_be_present_in_element((By.TAG_NAME, "body"), CONFIRMATION_TEXT))
      except TimeoutException as exc:
        raise SubmissionUnknown("form confirmation was not observed") from exc
      return {
        "demo": True,
        "message": "Demo completed — no parking purchased.",
        "confirmation": CONFIRMATION_TEXT,
        "completed_unix_ms": time.time_ns() // 1_000_000,
      }
    finally:
      driver.quit()
