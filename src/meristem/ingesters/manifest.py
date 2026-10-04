"""Manifest ingester — language-specific package manifests → atoms.

Emits three flavors of atom:

* ``dependency`` — one per declared package (topic_key ``dep:<ecosystem>:<name>``)
* ``runtime``    — language version pin from the manifest (e.g. requires-python)
* ``build_recipe`` — declared scripts (npm scripts, pyproject.scripts)

Stdlib-only: parsing is intentionally minimal so the toolchain stays small.
We trade some precision (e.g. we don't resolve workspace-style monorepos)
for portability across projects.
"""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Callable
from pathlib import Path

from ..atoms import Liveness, assert_fact
from .base import IngestContext, IngestResult, token_pattern

# Atoms per ingest run, across every manifest file combined. Each per-format
# parser's dependency loop had no bound of its own — a single package.json or
# requirements.txt listing hundreds of packages had nothing capping how many
# atoms one file could emit, unlike symbols.py's MAX_ATOMS for the same shape
# of "unbounded declarations in one file" risk.
MAX_ATOMS = 300


def run(ctx: IngestContext) -> IngestResult:
    res = IngestResult(name="manifest")
    handlers: list[tuple[str, Callable[[IngestContext, Path, IngestResult], None]]] = [
        ("pyproject.toml", _pyproject),
        ("package.json", _package_json),
        ("pubspec.yaml", _pubspec_yaml),
        ("Cargo.toml", _cargo_toml),
        ("requirements.txt", _requirements_txt),
        ("go.mod", _go_mod),
    ]
    found = False
    for name, fn in handlers:
        path = ctx.root / name
        if not path.is_file():
            continue
        found = True
        if not ctx.is_changed(name):  # incremental: skip manifests that didn't change
            continue
        try:
            fn(ctx, path, res)
        except Exception as exc:  # noqa: BLE001
            res.notes.append(f"{name}: {type(exc).__name__}: {exc}")
            # Paired with the note, like every sibling ingester's per-file
            # exception handler (readme.py, schema_sql.py, invariants.py) — a
            # malformed manifest is a skipped file, not just a log line, and
            # `atoms_skipped` is the signal `doctor`'s `ingest.notes` check
            # actually gates on (a purely informational note alone does not
            # warrant a warning; see that check's docstring).
            res.atoms_skipped += 1
    if not found:
        res.notes.append("no manifests at root")
    if res.atoms_inserted >= MAX_ATOMS:
        res.notes.append(f"capped at {MAX_ATOMS} atoms")
        res.truncated = True
    return res


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _emit_dep(
    ctx: IngestContext, res: IngestResult, *,
    ecosystem: str, name: str, spec: str, manifest_rel: str,
) -> bool:
    """Emit one dependency atom, or refuse once MAX_ATOMS is reached.

    Every parser below funnels its per-declaration loop through this (and the
    two siblings below) rather than incrementing `res.atoms_inserted` at each
    call site, so the cap is enforced in exactly one place regardless of which
    manifest format is being parsed.
    """
    if res.atoms_inserted >= MAX_ATOMS:
        return False
    assert_fact(
        ctx.conn,
        type="dependency",
        topic_key=f"dep:{ecosystem}:{name}",
        summary_10w=f"{ecosystem} dep {name}",
        summary_50w=f"{ecosystem} dependency {name} pinned at {spec} (from {manifest_rel}).",
        source_kind="manifest",
        source_ref=manifest_rel,
        liveness=Liveness(
            kind="regex", target=manifest_rel, pattern=token_pattern(name)
        ),
        repo_id=ctx.repo_id,
        workspace_id=ctx.workspace_id,
    )
    res.atoms_inserted += 1
    return True


def _emit_runtime(
    ctx: IngestContext, res: IngestResult, *,
    lang: str, version: str, manifest_rel: str,
) -> bool:
    if res.atoms_inserted >= MAX_ATOMS:
        return False
    assert_fact(
        ctx.conn,
        type="runtime",
        topic_key=f"runtime:{lang}",
        summary_10w=f"{lang} {version}",
        summary_50w=f"{lang} runtime constraint: {version} (declared in {manifest_rel}).",
        source_kind="manifest",
        source_ref=manifest_rel,
        liveness=Liveness(
            kind="regex", target=manifest_rel, pattern=re.escape(version.strip(" \"'"))
        ),
        repo_id=ctx.repo_id,
        workspace_id=ctx.workspace_id,
    )
    res.atoms_inserted += 1
    return True


def _emit_script(
    ctx: IngestContext, res: IngestResult, *,
    ecosystem: str, name: str, body: str, manifest_rel: str,
) -> bool:
    if res.atoms_inserted >= MAX_ATOMS:
        return False
    assert_fact(
        ctx.conn,
        type="build_recipe",
        topic_key=f"script:{ecosystem}:{name}",
        summary_10w=f"{ecosystem} script {name}",
        summary_50w=f"{ecosystem} script `{name}` runs: {body}",
        source_kind="manifest",
        source_ref=manifest_rel,
        liveness=Liveness(
            kind="regex", target=manifest_rel, pattern=token_pattern(name)
        ),
        repo_id=ctx.repo_id,
        workspace_id=ctx.workspace_id,
    )
    res.atoms_inserted += 1
    return True


# ---------------------------------------------------------------------------
# per-format parsers
# ---------------------------------------------------------------------------

def _pyproject(ctx: IngestContext, path: Path, res: IngestResult) -> None:
    rel = ctx.rel_path(path)
    data = tomllib.loads(path.read_text(encoding="utf-8", errors="replace"))
    project = data.get("project", {}) or {}
    py = project.get("requires-python")
    if py:
        _emit_runtime(ctx, res, lang="python", version=str(py), manifest_rel=rel)
    for dep in project.get("dependencies", []) or []:
        name, spec = _split_pep508(str(dep))
        if not _emit_dep(ctx, res, ecosystem="pypi", name=name, spec=spec or "*", manifest_rel=rel):
            return
    optional = project.get("optional-dependencies", {}) or {}
    for extra, deps in optional.items():
        for dep in deps:
            name, spec = _split_pep508(str(dep))
            if not _emit_dep(
                ctx, res, ecosystem="pypi", name=name,
                spec=f"{spec or '*'} (extra:{extra})", manifest_rel=rel,
            ):
                return
    for script, target in (project.get("scripts", {}) or {}).items():
        if not _emit_script(
            ctx, res, ecosystem="pypi", name=script, body=str(target), manifest_rel=rel,
        ):
            return


_PEP508_RE = re.compile(r"^\s*([A-Za-z0-9_.\-]+)\s*(.*)$")


def _split_pep508(spec: str) -> tuple[str, str]:
    m = _PEP508_RE.match(spec)
    if not m:
        return spec, ""
    name = m.group(1)
    rest = m.group(2).strip()
    return name, rest


def _package_json(ctx: IngestContext, path: Path, res: IngestResult) -> None:
    rel = ctx.rel_path(path)
    data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    engines = (data.get("engines") or {})
    node_ver = engines.get("node")
    if node_ver:
        _emit_runtime(ctx, res, lang="node", version=str(node_ver), manifest_rel=rel)
    for section in ("dependencies", "devDependencies", "peerDependencies"):
        for name, spec in (data.get(section) or {}).items():
            label = f"{spec}" if section == "dependencies" else f"{spec} ({section})"
            if not _emit_dep(ctx, res, ecosystem="npm", name=name, spec=label, manifest_rel=rel):
                return
    for name, body in (data.get("scripts") or {}).items():
        if not _emit_script(
            ctx, res, ecosystem="npm", name=name, body=str(body), manifest_rel=rel,
        ):
            return


_YAML_DEP_RE = re.compile(r"^\s{2}([A-Za-z0-9_]+):\s*(.+)$")


def _pubspec_yaml(ctx: IngestContext, path: Path, res: IngestResult) -> None:
    rel = ctx.rel_path(path)
    text = path.read_text(encoding="utf-8", errors="replace")
    # Very narrow YAML: only catches `name: spec` lines under `dependencies:`/
    # `dev_dependencies:` sections. Nested map specs (git/path) are kept as
    # the literal text of the first line — good enough for liveness pinning.
    section: str | None = None
    for line in text.splitlines():
        s = line.rstrip()
        if not s or s.startswith("#"):
            continue
        if not line.startswith(" "):
            head = s.split(":", 1)[0]
            section = head if head in {"dependencies", "dev_dependencies"} else None
            if head == "environment":
                section = "environment"
            continue
        if section in {"dependencies", "dev_dependencies"}:
            m = _YAML_DEP_RE.match(line)
            if m and not _emit_dep(
                ctx, res, ecosystem="pub", name=m.group(1),
                spec=m.group(2).strip() or "*", manifest_rel=rel,
            ):
                return
        elif section == "environment":
            m = _YAML_DEP_RE.match(line)
            if m and m.group(1) in {"sdk", "flutter"} and not _emit_runtime(
                ctx, res, lang=("dart" if m.group(1) == "sdk" else "flutter"),
                version=m.group(2).strip(), manifest_rel=rel,
            ):
                return


def _cargo_toml(ctx: IngestContext, path: Path, res: IngestResult) -> None:
    rel = ctx.rel_path(path)
    data = tomllib.loads(path.read_text(encoding="utf-8", errors="replace"))
    pkg = data.get("package", {}) or {}
    edition = pkg.get("edition") or pkg.get("rust-version")
    if edition:
        _emit_runtime(ctx, res, lang="rust", version=str(edition), manifest_rel=rel)
    for section in ("dependencies", "dev-dependencies", "build-dependencies"):
        for name, spec in (data.get(section) or {}).items():
            label = spec if isinstance(spec, str) else json.dumps(spec, sort_keys=True)
            if section != "dependencies":
                label = f"{label} ({section})"
            if not _emit_dep(
                ctx, res, ecosystem="cargo", name=name, spec=str(label), manifest_rel=rel,
            ):
                return


_REQ_LINE_RE = re.compile(r"^([A-Za-z0-9_.\-]+)\s*(.*)$")


def _requirements_txt(ctx: IngestContext, path: Path, res: IngestResult) -> None:
    rel = ctx.rel_path(path)
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        m = _REQ_LINE_RE.match(line)
        if not m:
            continue
        if not _emit_dep(
            ctx, res, ecosystem="pypi", name=m.group(1),
            spec=m.group(2).strip() or "*", manifest_rel=rel,
        ):
            return


_GO_DEP_RE = re.compile(r"^\s*([\w./\-]+)\s+(v\S+)")


def _go_mod(ctx: IngestContext, path: Path, res: IngestResult) -> None:
    rel = ctx.rel_path(path)
    in_require = False
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = raw.strip()
        if s.startswith("go "):
            if not _emit_runtime(ctx, res, lang="go", version=s[3:].strip(), manifest_rel=rel):
                return
            continue
        if s.startswith("require ("):
            in_require = True
            continue
        if in_require and s == ")":
            in_require = False
            continue
        target = s if in_require else (s[len("require "):] if s.startswith("require ") else "")
        if not target:
            continue
        m = _GO_DEP_RE.match(target)
        if m and not _emit_dep(
            ctx, res, ecosystem="gomod", name=m.group(1),
            spec=m.group(2), manifest_rel=rel,
        ):
            return
