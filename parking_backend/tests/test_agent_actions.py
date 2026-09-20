import unittest

from parking_backend.agent.actions import MalformedAction, parse_action


class TestActionSchema(unittest.TestCase):
  def test_a_well_formed_action_parses(self):
    action = parse_action('{"action": "TAP", "nid": "n7", "why": "continue"}')
    self.assertEqual((action.kind, action.nid, action.why), ("TAP", "n7", "continue"))

  def test_fenced_json_is_tolerated(self):
    self.assertEqual(parse_action('```json\n{"action": "BACK"}\n```').kind, "BACK")

  def test_prose_and_broken_json_are_refused(self):
    for raw in ("I will now tap the button", "", "{", "[]", "null", '{"nid": "n1"}'):
      with self.subTest(raw=raw), self.assertRaises(MalformedAction):
        parse_action(raw)

  def test_an_action_outside_the_schema_is_refused(self):
    for kind in ("EXEC", "RUN_JS", "ADB", "tap", "OPEN_APP"):
      with self.subTest(kind=kind), self.assertRaises(MalformedAction):
        parse_action({"action": kind})

  def test_extra_keys_are_refused_rather_than_ignored(self):
    # A silently dropped key is how a model smuggles intent past a schema.
    with self.assertRaises(MalformedAction):
      parse_action({"action": "TAP", "nid": "n1", "script": "alert(1)"})

  def test_unknown_profile_fields_and_secret_slots_are_refused(self):
    with self.assertRaises(MalformedAction):
      parse_action({"action": "FILL_PROFILE", "nid": "n1", "field": "password"})
    with self.assertRaises(MalformedAction):
      parse_action({"action": "FILL_SECRET", "slot": "card_pin"})

  def test_waiting_is_bounded(self):
    self.assertEqual(parse_action({"action": "WAIT", "seconds": 5}).seconds, 5)
    for seconds in (0, 6, 3600, "3", True):
      with self.subTest(seconds=seconds), self.assertRaises(MalformedAction):
        parse_action({"action": "WAIT", "seconds": seconds})

  def test_typing_an_empty_string_is_allowed(self):
    # Clearing a prefilled field is a legitimate move.
    self.assertEqual(parse_action({"action": "TYPE", "nid": "n1", "text": ""}).text, "")

  def test_overlong_values_are_refused(self):
    with self.assertRaises(MalformedAction):
      parse_action({"action": "TYPE", "nid": "n1", "text": "x" * 500})
    with self.assertRaises(MalformedAction):
      parse_action({"action": "OPEN_URL", "url": "https://x.example/" + "a" * 600})

  def test_scroll_directions_are_constrained(self):
    self.assertEqual(parse_action({"action": "SCROLL", "direction": "down"}).direction, "down")
    with self.assertRaises(MalformedAction):
      parse_action({"action": "SCROLL", "direction": "sideways"})

  def test_intervention_codes_are_constrained(self):
    self.assertEqual(parse_action({"action": "REQUEST_USER", "code": "CAPTCHA"}).code, "CAPTCHA")
    with self.assertRaises(MalformedAction):
      parse_action({"action": "REQUEST_USER", "code": "JUST_DO_IT"})

  def test_a_checkout_report_carries_its_figures(self):
    action = parse_action({
      "action": "READY_TO_PURCHASE", "merchant": "Example Garage", "location_label": "123 Main St",
      "plate": "DEMO123", "duration_seconds": 10800, "total_minor": 1450, "currency": "usd",
      "line_items": [["Parking", 1200], ["Fee", 250]], "pay_nid": "n3",
    })
    self.assertEqual((action.total_minor, action.currency), (1450, "USD"))
    self.assertEqual(action.line_items, (("Parking", 1200), ("Fee", 250)))

  def test_malformed_line_items_and_currencies_are_refused(self):
    base = {"action": "READY_TO_PURCHASE", "merchant": "M", "pay_nid": "n1"}
    with self.assertRaises(MalformedAction):
      parse_action({**base, "line_items": [["Parking"]]})
    with self.assertRaises(MalformedAction):
      parse_action({**base, "currency": "dollars"})
    with self.assertRaises(MalformedAction):
      parse_action({**base, "total_minor": "1450"})

  def test_booleans_are_not_accepted_as_numbers(self):
    with self.assertRaises(MalformedAction):
      parse_action({"action": "READY_TO_PURCHASE", "merchant": "M", "pay_nid": "n1", "total_minor": True})


if __name__ == "__main__":
  unittest.main()
