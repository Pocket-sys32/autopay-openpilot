"""A scripted browser for exercising the agent loop with no emulator and no network."""
from __future__ import annotations

from dataclasses import dataclass, field

from parking_backend.agent.types import Node, Observation


@dataclass
class Page:
  url: str
  text: str = ""
  nodes: tuple[Node, ...] = ()
  title: str = ""
  # What tapping a given nid leads to; anything unmapped leaves the page unchanged.
  taps: dict[str, str] = field(default_factory=dict)


class FakeBrowser:
  def __init__(self, pages: dict[str, Page], start: str):
    self.pages = pages
    self.current = start
    self.typed: list[tuple[str, str]] = []
    self.secrets: list[tuple[str, str]] = []
    self.tapped: list[str] = []
    self.selected: list[tuple[str, str]] = []
    self.scrolled: list[tuple[str, str]] = []
    self.backs = 0
    self.waits: list[int] = []
    self.observations = 0

  @property
  def page(self) -> Page:
    return self.pages[self.current]

  def observe(self, step: int) -> Observation:
    self.observations += 1
    page = self.page
    from urllib.parse import urlsplit
    return Observation(step=step, url=page.url, host=urlsplit(page.url).hostname or "", title=page.title,
                       nodes=page.nodes, text_digest=page.text, screenshot_jpeg=b"jpeg")

  def open_url(self, url: str) -> None:
    self.current = url if url in self.pages else self.current

  def tap(self, nid: str) -> None:
    self.tapped.append(nid)
    destination = self.page.taps.get(nid)
    if destination:
      self.current = destination

  def type_text(self, nid: str, text: str) -> None:
    self.typed.append((nid, text))

  def fill_secret(self, slot: str, value: str) -> None:
    self.secrets.append((slot, value))

  def select(self, nid: str, option_text: str) -> None:
    self.selected.append((nid, option_text))

  def scroll(self, direction: str, nid: str = "") -> None:
    self.scrolled.append((direction, nid))

  def back(self) -> None:
    self.backs += 1

  def wait(self, seconds: int) -> None:
    self.waits.append(seconds)
