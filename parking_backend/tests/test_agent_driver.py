import unittest
from unittest import mock

from parking_backend.agent.driver import _wait_for_security_verification


class FakeDriver:
  def __init__(self, titles):
    self.titles = list(titles)
    self.last_title = self.titles[-1]
    self.refreshes = 0

  @property
  def title(self):
    if self.titles:
      self.last_title = self.titles.pop(0)
    return self.last_title

  def refresh(self):
    self.refreshes += 1


class TestSecurityVerificationWait(unittest.TestCase):
  @mock.patch("parking_backend.agent.driver.time.sleep")
  def test_returns_when_the_transient_page_clears(self, sleep):
    driver = FakeDriver(["Just a moment...", "Just a moment...", "LAZ Parking"])
    _wait_for_security_verification(driver)
    self.assertEqual(sleep.call_count, 2)
    self.assertEqual(driver.refreshes, 0)

  @mock.patch("parking_backend.agent.driver.time.sleep")
  def test_refreshes_once_but_remains_bounded(self, sleep):
    driver = FakeDriver(["Just a moment..."])
    _wait_for_security_verification(driver)
    self.assertEqual(driver.refreshes, 1)
    self.assertEqual(sleep.call_count, 24)

  @mock.patch("parking_backend.agent.driver.time.sleep")
  def test_an_ordinary_page_returns_immediately(self, sleep):
    driver = FakeDriver(["Parking checkout"])
    _wait_for_security_verification(driver)
    sleep.assert_not_called()


if __name__ == "__main__":
  unittest.main()
