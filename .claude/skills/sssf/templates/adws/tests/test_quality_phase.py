"""Quality blocks attribute their evidence to the phase passed in — never to
whatever phase happens to be last."""

from pathlib import Path

from adw_modules.data_types import Phase, PhaseParams

CFG = "adws/adw_sssf_config/sssf.config.yaml"


def _phase(seq: int) -> Phase:
    return Phase(
        phase_id=f"q0000000_{seq:02d}_probe", adw_id="q0000000", seq=seq,
        params=PhaseParams(name="probe", kind="code", owner="quality",
                           description="Probe that quality evidence lands under the given phase"))


def test_check_dir_uses_the_passed_phase_not_the_last(repo, modules):
    cfg = modules["agents"].load_config(CFG)
    run = modules["session"].ensure(cfg, "q0000000")
    result = modules["quality"].test(run, _phase(3))
    assert result.passed
    assert (run.context_handoff_dir / "quality" / "03_test" / "command.log").is_file()
    assert not (run.context_handoff_dir / "quality" / "00_test").exists()


def test_run_quality_runs_every_block_under_the_phase(repo, modules):
    cfg = modules["agents"].load_config(CFG)
    run = modules["session"].ensure(cfg, "q1111111")
    result = modules["quality"].run_quality(run, _phase(7))
    assert result.passed and len(result.checks) == 4
    names = sorted(p.name for p in (run.context_handoff_dir / "quality" / "07_test").parent.iterdir())
    assert names == ["07_build", "07_lint", "07_test", "07_typecheck"], names
    assert Path(result.artifacts[0]).is_absolute()
