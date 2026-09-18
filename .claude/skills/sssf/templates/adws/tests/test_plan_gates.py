"""Plan gates: shape checks on the planner's report — never quality judgments."""

from adw_modules.data_types import PlanOutput
from adw_modules import gates


def _plan(**kw):
    args = {"status": "success", "summary": "Add a /health endpoint.",
            "artifacts": ["specs/a1b2c3d4_health.md"],
            "notes_for_next_agent": "Implement exactly; the route returns 200 with an empty body."}
    args.update(kw)
    return PlanOutput(**args)


def test_valid_plan_passes_all_three():
    envelope = _plan()
    assert gates.plan_declares_artifacts(envelope, None).passed
    assert gates.plan_summary_present(envelope, None).passed
    assert gates.plan_handoff_present(envelope, None).passed


def test_empty_artifacts_fails():
    report = gates.plan_declares_artifacts(_plan(artifacts=[]), None)
    assert not report.passed and len(report.violations) == 1


def test_blank_summary_fails():
    assert not gates.plan_summary_present(_plan(summary="   "), None).passed


def test_blank_handoff_fails():
    assert not gates.plan_handoff_present(_plan(notes_for_next_agent=""), None).passed


def test_passing_checks_carry_evidence():
    report = gates.plan_summary_present(_plan(), None)
    assert report.checks[0].note != ""
