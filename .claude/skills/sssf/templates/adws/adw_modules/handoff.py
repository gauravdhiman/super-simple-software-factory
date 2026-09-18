"""Sealed handoffs: the build proves it implements the reviewed plan.

A plan phase ends with artifacts on disk; a build phase starts from them. In
between — a commit, a rebase, a second ADW joining under the same id days
later, another parallel run in the same repo — those bytes can change without
anyone noticing, and the builder quietly implements a spec nobody reviewed.

Sealing closes that gap with fingerprints, not trust. `seal_artifacts` hashes
every declared artifact and persists the seal beside the session (so a later
process joining the same adw_id verifies against the same seal);
`verify_artifacts` re-hashes and refuses to start the build when anything
moved, naming every changed or vanished file. Added files are the builder's
job and never violate a seal — only the sealed bytes are protected.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from .data_types import HandoffSeal
from .utils import now_iso, repo_path


def _key(root: Path, path: Path) -> str:
    """Repo-relative posix key when inside the worktree, absolute otherwise."""
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _resolve(root: Path, key: str) -> Path:
    path = Path(key)
    return path if path.is_absolute() else root / path


def _sidecar(run, label: str) -> Path:
    return Path(run.session_dir) / f"handoff_{label}.json"


def seal_artifacts(run, label: str, artifacts: list[str] | None) -> HandoffSeal:
    """Fingerprint `artifacts` now. Fails fast on an empty list or a file that
    is not there — sealing a lie is worse than not sealing."""
    if not artifacts:
        raise RuntimeError(f"nothing to seal under {label!r} — a handoff with no artifacts is malformed")
    root = Path(run.repo_root)
    files: dict[str, str] = {}
    for declared in artifacts:
        path = repo_path(root, declared)
        if not (path.exists() and path.is_file()):
            raise RuntimeError(
                f"cannot seal {declared!r} under {label!r} — no such file in {root}")
        files[_key(root, path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    seal = HandoffSeal(label=label, adw_id=run.adw_id, files=files, sealed_at=now_iso())
    _sidecar(run, label).write_text(seal.model_dump_json(indent=2))
    return seal


def verify_artifacts(run, label: str) -> HandoffSeal:
    """Re-hash the sealed files. Returns the seal when every byte matches;
    raises naming every changed or vanished file otherwise — the build must
    not start from a spec nobody reviewed."""
    sidecar = _sidecar(run, label)
    if not sidecar.is_file():
        raise RuntimeError(
            f"no seal under {label!r} — run the seal phase first (or re-run the "
            f"planning ADW under the same --adw-id to reseal)")
    seal = HandoffSeal.model_validate_json(sidecar.read_text())
    root = Path(run.repo_root)
    changed = [key for key, digest in sorted(seal.files.items())
               if not _resolve(root, key).is_file()
               or hashlib.sha256(_resolve(root, key).read_bytes()).hexdigest() != digest]
    if changed:
        detail = "\n".join(f"  - {key}" for key in changed)
        raise RuntimeError(
            f"handoff {label!r} no longer matches its seal ({digest_of(seal)}) — "
            f"{len(changed)} file(s) changed or vanished since:\n{detail}\n"
            f"Re-run the planning ADW under --adw-id {run.adw_id} to review and reseal; "
            f"do not build from an unreviewed spec.")
    return seal


def digest_of(seal: HandoffSeal) -> str:
    """One short id for the whole seal — what the trace shows."""
    combined = "\n".join(f"{key}={seal.files[key]}" for key in sorted(seal.files))
    return hashlib.sha256(combined.encode()).hexdigest()[:12]
