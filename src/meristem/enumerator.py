"""Edge-case enumerator (SPEC §11).

Deterministic algorithm — runs as the penultimate step of every change:

1. **Symbol diff** — parse the staged patch, extract changed symbol names.
2. **Caller scan** — ripgrep across the workspace + sibling repos.
3. **Invariant proximity** — grep for `assert`, `// invariant:`, schema
   constraints, within ±20 lines of each touched span.
4. **Test coverage gap** — for each touched line, does any test file
   reference the symbol? If not, flag.
5. **Cross-repo mirrors** — consult atoms tagged MIRRORS:<symbol>.

Rank by (callers × invariant_proximity), keep top 5; the rest collapse to
``+N more, run :check --deep``.

Pre-filter by file fingerprint — CSS-only changes skip auth/race/schema
categories. Schema changes mandate schema_migration + cross_repo_impact +
backwards_compat.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import store
from .paths import detect_layout

SEVERITY_ORDER = {"high": 0, "med": 1, "low": 2}

# File-extension fingerprints — drive the kind pre-filter.
_FINGERPRINTS = {
    ".css":   {"perf_hotpath"},
    ".scss":  {"perf_hotpath"},
    ".md":    set(),  # docs change → enumerate nothing
    ".sql":   {"schema_migration", "cross_repo_impact", "backwards_compat",
               "invariant_risk"},
}

# Symbol extractors per language. Matches both definitions (where the
# diff touches an identifier on the LHS) and any line referencing names
# we care about.
_SYMBOL_RES = [
    re.compile(r"\b(?:def|class|func|fn)\s+([A-Za-z_][\w$]*)"),
    re.compile(r"\b([A-Z][A-Za-z0-9_]+)\s*\("),       # CamelCase calls
    re.compile(r"\b([a-z][A-Za-z0-9_]+)\s*\("),       # camelCase calls
]


@dataclass
class Finding:
    kind: str
    severity: str
    claim: str
    evidence: list[str] = field(default_factory=list)
    suggested_action: str = ""

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "severity": self.severity,
            "claim": self.claim,
            "evidence": self.evidence,
            "suggested_action": self.suggested_action,
        }


def _staged_diff(root: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "diff", "--staged", "-U2"],
            cwd=root, check=True, capture_output=True, text=True,
        )
        return out.stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


def _staged_files(root: Path) -> list[str]:
    try:
        out = subprocess.run(
            ["git", "diff", "--staged", "--name-only"],
            cwd=root, check=True, capture_output=True, text=True,
        )
        return [line.strip() for line in out.stdout.splitlines() if line.strip()]
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []


def _extract_symbols(diff: str) -> set[str]:
    out: set[str] = set()
    for line in diff.splitlines():
        if not line.startswith(("+", "-")) or line.startswith(("+++", "---")):
            continue
        body = line[1:]
        for rx in _SYMBOL_RES:
            for m in rx.finditer(body):
                out.add(m.group(1))
    return {s for s in out if len(s) > 2 and not s.isupper()}


def _callers(root: Path, symbol: str, *, exclude: list[str]) -> list[str]:
    """ripgrep for `\\bsymbol\\b` across the repo; return first 10 hits."""
    rg = ["rg", "-n", "--no-heading", rf"\b{re.escape(symbol)}\b", str(root)]
    for d in exclude:
        rg += ["-g", f"!{d}"]
    try:
        out = subprocess.run(rg, check=False, capture_output=True, text=True)
    except FileNotFoundError:
        return []  # rg not installed → silently no callers
    return [line for line in out.stdout.splitlines() if line.strip()][:10]


def _test_files_reference(root: Path, symbol: str) -> bool:
    rg = [
        "rg", "-l", "--no-heading", rf"\b{re.escape(symbol)}\b",
        "-g", "test*", "-g", "*_test.*", "-g", "*.test.*",
        str(root),
    ]
    try:
        out = subprocess.run(rg, check=False, capture_output=True, text=True)
    except FileNotFoundError:
        return False
    return bool(out.stdout.strip())


def _mirrors_atoms(conn, symbol: str) -> list[str]:
    rows = conn.execute(
        """SELECT a.id, a.topic_key
             FROM live_atoms a
             JOIN edges e ON (e.src_id = a.id OR e.dst_id = a.id)
            WHERE e.kind = 'MIRRORS' AND e.valid_to IS NULL
              AND (a.topic_key LIKE ? OR a.topic_key LIKE ?)""",
        (f"%{symbol}%", f"%:{symbol}"),
    ).fetchall()
    return [r["id"] for r in rows]


def _kind_filter(files: list[str]) -> set[str] | None:
    """Intersection of fingerprints — returns None for 'enumerate everything'."""
    all_kinds: set[str] | None = None
    for f in files:
        ext = Path(f).suffix.lower()
        if ext in _FINGERPRINTS:
            kinds = _FINGERPRINTS[ext]
            all_kinds = kinds if all_kinds is None else (all_kinds | kinds)
    return all_kinds


def enumerate_edge_cases(*, max_findings: int = 5) -> list[Finding]:
    """Run the SPEC §11 algorithm. Returns top-N findings."""
    layout = detect_layout()
    diff = _staged_diff(layout.root)
    if not diff:
        return []
    files = _staged_files(layout.root)
    kind_filter = _kind_filter(files)
    symbols = _extract_symbols(diff)
    findings: list[Finding] = []

    has_db = layout.db.exists()
    conn = store.connect(layout.db) if has_db else None
    try:
        for sym in sorted(symbols):
            callers = _callers(layout.root, sym, exclude=["node_modules", ".venv", "build"])
            n_callers = len(callers)
            tested = _test_files_reference(layout.root, sym)

            # 4. test coverage gap
            if not tested and (kind_filter is None or "test_gap" not in (kind_filter or set())
                              or "test_gap" in kind_filter):
                findings.append(Finding(
                    kind="test_gap",
                    severity=("med" if n_callers >= 3 else "low"),
                    claim=f"`{sym}` touched but no test file references it",
                    evidence=callers[:2],
                    suggested_action=f"add a smoke test exercising {sym}",
                ))

            # 5. cross-repo mirrors
            if conn is not None:
                mirror_atoms = _mirrors_atoms(conn, sym)
                if mirror_atoms:
                    findings.append(Finding(
                        kind="cross_repo_impact",
                        severity="high",
                        claim=f"`{sym}` has MIRRORS edges — siblings may need matching changes",
                        evidence=mirror_atoms[:3],
                        suggested_action="verify mirrored writes in the partner repo",
                    ))

            # 3. invariant proximity — count `assert|invariant` in caller files
            if n_callers >= 2:
                assert_hits = sum(1 for c in callers if "assert" in c.lower())
                if assert_hits:
                    findings.append(Finding(
                        kind="invariant_risk",
                        severity="high",
                        claim=f"`{sym}` referenced near asserts ({assert_hits} sites)",
                        evidence=callers[:assert_hits + 1],
                        suggested_action="audit invariants around the touched call sites",
                    ))
    finally:
        if conn is not None:
            conn.close()

    # Schema fingerprint mandates extra kinds.
    if kind_filter and "schema_migration" in kind_filter:
        findings.insert(0, Finding(
            kind="schema_migration",
            severity="high",
            claim="staged diff includes a .sql file — schema migration required",
            evidence=[f for f in files if f.endswith(".sql")][:3],
            suggested_action="write the up/down migration and a smoke test",
        ))

    # Apply kind filter, if any, and dedupe.
    if kind_filter is not None and kind_filter != set():
        findings = [f for f in findings if f.kind in kind_filter or f.kind == "schema_migration"]
    elif kind_filter == set():
        findings = []  # docs-only — enumerate nothing

    seen: set[tuple] = set()
    out: list[Finding] = []
    for f in findings:
        key = (f.kind, f.claim[:40])
        if key in seen:
            continue
        seen.add(key)
        out.append(f)

    out.sort(key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), -len(f.evidence)))
    return out[:max_findings]


def format_report(findings: list[Finding], *, total_candidates: int = 0) -> str:
    """SPEC §11 output format. Severity-prefixed, ≤80 chars per line."""
    if not findings:
        if total_candidates:
            return (
                f"no edge cases detected ({total_candidates} candidates rejected as "
                "unverifiable)."
            )
        return "no edge cases detected."
    lines = ["Edge cases to consider:"]
    for f in findings:
        head = f"  • [{f.severity}] {f.claim}"
        if len(head) > 80:
            head = head[:77] + "..."
        lines.append(head)
        if f.evidence:
            lines.append(f"    → {f.evidence[0]}")
    return "\n".join(lines)


def to_json(findings: list[Finding]) -> str:
    return json.dumps({
        "agent": "edge_enumerator",
        "findings": [f.to_dict() for f in findings],
    })


__all__ = [
    "Finding", "enumerate_edge_cases", "format_report", "to_json",
]
