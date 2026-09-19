import json

from openpilot.common.test import OpenpilotTestCase
from openpilot.selfdrive.parking.reasoner_client import parse_reasoner_assessment


class TestReasonerClient(OpenpilotTestCase):
  def test_accepts_bounded_advisory_result(self):
    assessment = parse_reasoner_assessment(json.dumps({
      "candidate_consistent": True,
      "likely_parked": True,
      "conflicts": [],
      "summary": "QR and parking context agree.",
    }))
    self.assertTrue(assessment.advisory_only)

  def test_rejects_unknown_fields_and_conflicts(self):
    with self.assertRaises(ValueError):
      parse_reasoner_assessment({
        "candidate_consistent": True,
        "likely_parked": True,
        "conflicts": [],
        "summary": "ok",
        "pay": True,
      })
    with self.assertRaises(ValueError):
      parse_reasoner_assessment({
        "candidate_consistent": True,
        "likely_parked": True,
        "conflicts": ["execute_payment"],
        "summary": "ok",
      })
