#!/usr/bin/env python3
"""ttb_verify.py 3.2.0 (2026-06-09) — offline TTB label pre-check, single file, stdlib only.

Compares filed COLA application data against the transcribed text of a label.
The transcription (from the paper label, a photo, or any OCR of the operator's
choosing) is read from a file or stdin; every pass/fail decision is made by
deterministic rules in this file, so verdicts are reproducible and auditable.

Exit codes: 0 = PASS, 1 = FAIL (or self-test failure), 2 = usage/input error,
3 = NEEDS_REVIEW (agent judgment required). In --batch mode the worst row wins:
any ERROR/FAIL -> 1, else any NEEDS_REVIEW -> 3, else 0.
"""

import argparse
import csv
import json
import os
import re
import sys
from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import StrEnum
from pathlib import Path

VERSION = "3.2.0"

# 27 CFR Part 16 — wording is statutory and must appear verbatim.
REQUIRED_WARNING_PREFIX = "GOVERNMENT WARNING:"
REQUIRED_WARNING_BODY = (
    "(1) According to the Surgeon General, women should not drink alcoholic "
    "beverages during pregnancy because of the risk of birth defects. "
    "(2) Consumption of alcoholic beverages impairs your ability to drive a car "
    "or operate machinery, and may cause health problems."
)
REQUIRED_WARNING = f"{REQUIRED_WARNING_PREFIX} {REQUIRED_WARNING_BODY}"

# Below this similarity a difference is a mismatch; above it, an agent should look.
FUZZY_REVIEW_THRESHOLD = 0.85
# Below this similarity a free-form field is reported as not found on the label.
LOCATOR_FLOOR = 0.55

_WS_RE = re.compile(r"\s+")
_PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_PROOF_RE = re.compile(r"(\d+(?:\.\d+)?)\s*-?\s*proof\b", re.IGNORECASE)
_NET_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(ml|cl|l|liters?|litres?|fl\.?\s*oz\.?|oz)\b", re.IGNORECASE
)
_PREFIX_RE = re.compile(r"^\s*government\s+warning\s*:?\s*", re.IGNORECASE)
_WARNING_START_RE = re.compile(r"government\s+warning", re.IGNORECASE)
_CHAR_MAP = str.maketrans(
    {"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2013": "-", "\u2014": "-", "`": "'"}
)


class Status(StrEnum):
    MATCH = "MATCH"
    MATCH_WITH_NOTE = "MATCH_WITH_NOTE"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    MISMATCH = "MISMATCH"
    NOT_FOUND = "NOT_FOUND"


@dataclass
class FieldResult:
    field: str
    status: Status
    expected: str = ""
    found: str = ""
    note: str = ""


@dataclass
class Application:
    brand: str
    class_type: str = ""
    alcohol: str = ""
    net: str = ""


def normalize_ws(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


def normalize_text(text: str) -> str:
    # Unify typographic quotes/dashes so punctuation style alone never fails a label.
    return normalize_ws(text.translate(_CHAR_MAP))


def parse_abv(text: str) -> float | None:
    match = _PCT_RE.search(text)
    if match:
        return float(match.group(1))
    match = _PROOF_RE.search(text)
    if match:
        # US proof is exactly twice ABV.
        return float(match.group(1)) / 2.0
    return None


def parse_net_contents_ml(text: str) -> float | None:
    match = _NET_RE.search(text)
    if not match:
        return None
    value = float(match.group(1))
    unit = re.sub(r"[^a-z]", "", match.group(2).lower())
    if unit.startswith("lit"):
        unit = "l"
    if unit in ("floz", "oz"):
        return value * 29.5735
    factor = {"ml": 1.0, "cl": 10.0, "l": 1000.0}.get(unit)
    return value * factor if factor else None


def best_window(tokens: list[str], expected: str) -> str | None:
    """Most similar contiguous word window to the expected text, case preserved."""
    exp_exact = " ".join(expected.split())
    exp = exp_exact.casefold()
    if not exp or not tokens:
        return None
    n = max(1, len(exp_exact.split()))
    best_text, best_key = None, (0.0, 0)
    for size in (n - 1, n, n + 1):
        if size < 1 or size > len(tokens):
            continue
        for start in range(len(tokens) - size + 1):
            window = " ".join(tokens[start : start + size])
            ratio = SequenceMatcher(None, exp, window.casefold()).ratio()
            # On equal similarity prefer the case-exact candidate.
            key = (ratio, int(window == exp_exact))
            if key > best_key:
                best_key, best_text = key, window
    return best_text if best_key[0] >= LOCATOR_FLOOR else None


def compare_text(name: str, expected: str, found: str | None) -> FieldResult:
    """Tiered text comparison: exact > case-only difference > near-miss > mismatch."""
    if found is None or not normalize_ws(found):
        return FieldResult(name, Status.NOT_FOUND, expected, note=f"{name} not found on the label.")
    expected_norm = normalize_text(expected)
    found_norm = normalize_text(found)
    if expected_norm == found_norm:
        return FieldResult(name, Status.MATCH, expected, found)
    if expected_norm.casefold() == found_norm.casefold():
        return FieldResult(
            name, Status.MATCH_WITH_NOTE, expected, found,
            "Same text; only capitalization or punctuation differs.",
        )
    ratio = SequenceMatcher(None, expected_norm.casefold(), found_norm.casefold()).ratio()
    if ratio >= FUZZY_REVIEW_THRESHOLD:
        return FieldResult(
            name, Status.NEEDS_REVIEW, expected, found,
            f"Similar but not identical ({ratio:.0%} similarity); agent judgment required.",
        )
    return FieldResult(name, Status.MISMATCH, expected, found, "Label text does not match the application.")


def compare_alcohol(expected: str, found_raw: str | None) -> FieldResult:
    """Numeric ABV comparison; understands % and proof so 45% == 90 Proof."""
    name = "Alcohol content"
    found = (found_raw or "").strip()
    if not found:
        return FieldResult(
            name, Status.NOT_FOUND, expected, note="No alcohol content statement found on the label."
        )
    expected_abv, label_abv = parse_abv(expected), parse_abv(found)
    if expected_abv is None or label_abv is None:
        # Cannot reduce to numbers; fall back to text comparison.
        return compare_text(name, expected, found)
    if abs(expected_abv - label_abv) <= 0.01:
        if normalize_text(expected).casefold() == normalize_text(found).casefold():
            return FieldResult(name, Status.MATCH, expected, found)
        return FieldResult(
            name, Status.MATCH_WITH_NOTE, expected, found,
            f"Values agree ({label_abv:g}% ABV); formatting differs.",
        )
    return FieldResult(
        name, Status.MISMATCH, expected, found,
        f"Application states {expected_abv:g}% ABV; label shows {label_abv:g}% ABV.",
    )


def compare_net_contents(expected: str, found_raw: str | None) -> FieldResult:
    """Unit-aware net contents comparison normalized to milliliters."""
    name = "Net contents"
    found = (found_raw or "").strip()
    if not found:
        return FieldResult(
            name, Status.NOT_FOUND, expected, note="No net contents statement found on the label."
        )
    expected_ml, found_ml = parse_net_contents_ml(expected), parse_net_contents_ml(found)
    if expected_ml is None or found_ml is None:
        return compare_text(name, expected, found)
    # 1% relative tolerance absorbs fl-oz/metric rounding on labels.
    if abs(expected_ml - found_ml) <= max(0.5, 0.01 * expected_ml):
        if normalize_text(expected).casefold() == normalize_text(found).casefold():
            return FieldResult(name, Status.MATCH, expected, found)
        return FieldResult(
            name, Status.MATCH_WITH_NOTE, expected, found,
            f"Values agree ({found_ml:g} mL); formatting differs.",
        )
    return FieldResult(
        name, Status.MISMATCH, expected, found,
        f"Application states {expected_ml:g} mL; label shows {found_ml:g} mL.",
    )


def _warning_mismatch(found: str, note: str) -> FieldResult:
    return FieldResult("Government warning", Status.MISMATCH, REQUIRED_WARNING, found, note)


def compare_warning(found: str | None, appears_bold: bool | None) -> FieldResult:
    """Statutory check: verbatim wording, all-caps header, bold header flag."""
    name = "Government warning"
    if found is None or not normalize_ws(found):
        return FieldResult(
            name, Status.NOT_FOUND, REQUIRED_WARNING,
            note="Mandatory government warning statement not found on the label.",
        )
    raw = normalize_text(found)
    prefix_match = _PREFIX_RE.match(raw)
    if not prefix_match:
        return _warning_mismatch(
            found, "Statement does not begin with the required 'GOVERNMENT WARNING:' header."
        )
    printed_prefix = normalize_ws(prefix_match.group(0))
    if printed_prefix != REQUIRED_WARNING_PREFIX:
        return _warning_mismatch(
            found,
            "Header must read exactly 'GOVERNMENT WARNING:' in capital letters; "
            f"label shows '{printed_prefix}'.",
        )
    body = normalize_ws(raw[prefix_match.end() :])
    if body.casefold() != REQUIRED_WARNING_BODY.casefold():
        return _warning_mismatch(found, _first_divergence(REQUIRED_WARNING_BODY, body))
    if appears_bold is False:
        return _warning_mismatch(
            found,
            "Wording is exact, but 'GOVERNMENT WARNING:' does not appear in bold type as required.",
        )
    if appears_bold is None:
        return FieldResult(
            name, Status.NEEDS_REVIEW, REQUIRED_WARNING, found,
            "Wording and capitalization are exact; confirm visually that the header is in bold type.",
        )
    return FieldResult(name, Status.MATCH, REQUIRED_WARNING, found)


def _first_divergence(required: str, actual: str) -> str:
    req_words, act_words = required.split(), actual.split()
    for i, (req, act) in enumerate(zip(req_words, act_words, strict=False)):
        if req.casefold() != act.casefold():
            context = " ".join(act_words[max(0, i - 3) : i + 4])
            return f"Wording deviates from the required statement near: '...{context}...'"
    return "Wording deviates from the required statement (text is truncated or extended)."


def overall_verdict(results: list[FieldResult]) -> str:
    statuses = {result.status for result in results}
    if Status.MISMATCH in statuses or Status.NOT_FOUND in statuses:
        return "FAIL"
    if Status.NEEDS_REVIEW in statuses:
        return "NEEDS_REVIEW"
    return "PASS"


def verify(app: Application, label_text: str, warning_bold: bool | None) -> tuple[str, list[FieldResult]]:
    """Locate each filed field in the label text and adjudicate; warning is always checked."""
    tokens = label_text.split()
    lines = [line for line in label_text.splitlines() if line.strip()]
    results = [compare_text("Brand name", app.brand, best_window(tokens, app.brand))]
    if app.class_type.strip():
        results.append(
            compare_text("Class/type", app.class_type, best_window(tokens, app.class_type))
        )
    if app.alcohol.strip():
        alcohol_line = next((line.strip() for line in lines if parse_abv(line) is not None), None)
        results.append(compare_alcohol(app.alcohol, alcohol_line))
    if app.net.strip():
        net_match = _NET_RE.search(label_text)
        results.append(compare_net_contents(app.net, net_match.group(0) if net_match else None))
    warning_match = _WARNING_START_RE.search(label_text)
    warning_text = (
        " ".join(label_text[warning_match.start() :].split()) if warning_match else None
    )
    results.append(compare_warning(warning_text, warning_bold))
    return overall_verdict(results), results


@dataclass
class RowResult:
    file: str
    overall: str
    fields: list[FieldResult]
    error: str = ""


_BOLD_FLAGS = {"yes": True, "no": False, "unknown": None, "": None}


def parse_manifest(path: Path) -> list[dict[str, str]]:
    """CSV with columns: file, brand (required); class_type, alcohol, net, warning_bold."""
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        names = {(name or "").strip().lower() for name in (reader.fieldnames or [])}
        missing = {"file", "brand"} - names
        if missing:
            raise ValueError(f"manifest missing required column(s): {', '.join(sorted(missing))}")
        rows = []
        for raw in reader:
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
            if row.get("file"):
                rows.append(row)
    if not rows:
        raise ValueError("manifest contains no data rows")
    return rows


def verify_row(row: dict[str, str], base: Path) -> RowResult:
    """Adjudicate one manifest row; per-row problems become ERROR rows, not batch failures."""
    name = row["file"]
    if not row.get("brand"):
        return RowResult(name, "ERROR", [], "manifest row has an empty brand")
    bold_raw = row.get("warning_bold", "").lower()
    if bold_raw not in _BOLD_FLAGS:
        return RowResult(name, "ERROR", [], f"warning_bold must be yes/no/unknown, not '{bold_raw}'")
    try:
        label_text = _read_label_text(str(base / name))
    except (OSError, ValueError) as exc:
        return RowResult(name, "ERROR", [], str(exc))
    app = Application(
        row["brand"], row.get("class_type", ""), row.get("alcohol", ""), row.get("net", "")
    )
    overall, results = verify(app, label_text, _BOLD_FLAGS[bold_raw])
    return RowResult(name, overall, results)


def batch_exit_code(results: list[RowResult]) -> int:
    overalls = {r.overall for r in results}
    if "ERROR" in overalls or "FAIL" in overalls:
        return 1
    if "NEEDS_REVIEW" in overalls:
        return 3
    return 0


def _row_findings(result: RowResult) -> str:
    if result.error:
        return result.error
    flagged = [f for f in result.fields if f.status != Status.MATCH]
    if not flagged:
        return "all fields match"
    return "; ".join(f"{f.field}: {f.status}" + (f" — {f.note}" if f.note else "") for f in flagged)


def render_batch(results: list[RowResult], color: bool) -> str:
    width = max((len(r.file) for r in results), default=4)
    lines = []
    for r in results:
        overall = _paint(f"{r.overall:<12}", "FAIL" if r.overall == "ERROR" else r.overall, color)
        lines.append(f"  {r.file:<{width}}  {overall}  {_row_findings(r)}")
    counts = {key: sum(1 for r in results if r.overall == key) for key in
              ("PASS", "NEEDS_REVIEW", "FAIL", "ERROR")}
    summary = ", ".join(f"{v} {k}" for k, v in counts.items() if v)
    return "\n".join([*lines, "", f"BATCH: {len(results)} labels — {summary}"])


def run_batch(manifest: Path, json_out: bool, color: bool) -> int:
    results = [verify_row(row, manifest.parent) for row in parse_manifest(manifest)]
    if json_out:
        payload = [
            {"file": r.file, "overall": r.overall, "error": r.error,
             "fields": [vars(f) for f in r.fields]}
            for r in results
        ]
        print(json.dumps(payload, indent=2))
    else:
        print(render_batch(results, color))
    return batch_exit_code(results)


_COLORS = {
    Status.MATCH: "32", Status.MATCH_WITH_NOTE: "32",
    Status.NEEDS_REVIEW: "33", Status.MISMATCH: "31", Status.NOT_FOUND: "31",
    "PASS": "32", "NEEDS_REVIEW": "33", "FAIL": "31",
}


def _paint(text: str, key, enable: bool) -> str:
    code = _COLORS.get(key)
    return f"\x1b[{code}m{text}\x1b[0m" if enable and code else text


def render(overall: str, results: list[FieldResult], color: bool) -> str:
    lines = [f"VERDICT: {_paint(overall, overall, color)}", ""]
    for r in results:
        status = _paint(f"{r.status:<15}", r.status, color)
        lines.append(f"  {r.field:<19} {status} {r.found or '—'}")
        if r.note:
            lines.append(f"  {'':<19} {'':<15} note: {r.note}")
    return "\n".join(lines)


def _read_label_text(source: str) -> str:
    text = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError("label text is empty")
    return text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Offline TTB label pre-check: compares filed application data "
        "against transcribed label text. Reads the label text from FILE or stdin.",
        epilog="Exit codes: 0 PASS, 1 FAIL, 2 usage/input error, 3 NEEDS_REVIEW.",
    )
    parser.add_argument("label", nargs="?", default="-", metavar="FILE",
                        help="label text file, or '-' for stdin (default)")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument("--brand", default="", help="brand name as filed (required unless --self-test)")
    parser.add_argument("--class-type", default="", help="class/type designation as filed")
    parser.add_argument("--alcohol", default="", help="alcohol content as filed, %% or proof")
    parser.add_argument("--net", default="", help="net contents as filed, e.g. '750 mL'")
    parser.add_argument("--warning-bold", choices=("yes", "no", "unknown"), default="unknown",
                        help="is the GOVERNMENT WARNING header printed in bold (default: unknown)")
    parser.add_argument("--batch", metavar="MANIFEST.csv",
                        help="batch mode: CSV of file,brand[,class_type,alcohol,net,warning_bold]; "
                        "label paths are relative to the manifest")
    parser.add_argument("--json", action="store_true", help="machine-readable JSON on stdout")
    parser.add_argument("--self-test", action="store_true", help="run the built-in test suite")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.self_test:
        return _self_test()
    color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    if args.batch:
        try:
            return run_batch(Path(args.batch), args.json, color)
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
    if not args.brand.strip():
        parser.error("--brand is required")
    try:
        label_text = _read_label_text(args.label)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))  # exits 2 with usage on stderr
    bold = _BOLD_FLAGS[args.warning_bold]
    app = Application(args.brand.strip(), args.class_type.strip(), args.alcohol.strip(), args.net.strip())
    overall, results = verify(app, label_text, bold)
    if args.json:
        print(json.dumps({"overall": overall, "fields": [vars(r) for r in results]}, indent=2))
    else:
        print(render(overall, results, color))
    return {"PASS": 0, "FAIL": 1, "NEEDS_REVIEW": 3}[overall]


# --- built-in test suite (stdlib only, no files) ---

SAMPLE_LABEL_TEXT = f"""OLD TOM DISTILLERY
Kentucky Straight Bourbon Whiskey
45% Alc./Vol. (90 Proof)
750 mL
{REQUIRED_WARNING_PREFIX} {REQUIRED_WARNING_BODY}"""

SAMPLE_APP = Application(
    brand="OLD TOM DISTILLERY",
    class_type="Kentucky Straight Bourbon Whiskey",
    alcohol="45% Alc./Vol. (90 Proof)",
    net="750 mL",
)


def _t_parsers() -> int:
    assert parse_abv("") is None and parse_abv("no numbers") is None
    assert parse_abv("45% Alc./Vol.") == 45.0
    assert parse_abv("90 Proof") == 45.0 and parse_abv("80-Proof") == 40.0
    assert parse_abv("45% Alc./Vol. (90 Proof)") == 45.0
    assert parse_net_contents_ml("") is None and parse_net_contents_ml("seven fifty") is None
    assert parse_net_contents_ml("750 mL") == 750.0 and parse_net_contents_ml("750ML") == 750.0
    assert parse_net_contents_ml("75 cl") == 750.0 and parse_net_contents_ml("1 L") == 1000.0
    floz = parse_net_contents_ml("25.4 fl. oz.")
    assert floz is not None and abs(floz - 751.17) < 0.1
    return 12


def _t_text_tiers() -> int:
    assert compare_text("Brand name", "OLD TOM", None).status == Status.NOT_FOUND
    assert compare_text("Brand name", "OLD TOM", "   ").status == Status.NOT_FOUND
    assert compare_text("Brand name", "OLD TOM", "OLD TOM").status == Status.MATCH
    case_only = compare_text("Brand name", "Stone's Throw", "STONE'S THROW")
    assert case_only.status == Status.MATCH_WITH_NOTE and "capitalization" in case_only.note
    assert compare_text("Brand name", "Stone's Throw", "Stone\u2019s Throw").status == Status.MATCH
    near_miss = compare_text("Brand name", "OLD TOM DISTILLERY", "OLD TON DISTILLERY")
    assert near_miss.status == Status.NEEDS_REVIEW
    assert compare_text("Brand name", "OLD TOM DISTILLERY", "RIVERBEND GIN CO").status == Status.MISMATCH
    return 8


def _t_numeric_fields() -> int:
    assert compare_alcohol("45%", None).status == Status.NOT_FOUND
    assert compare_alcohol("45%", "   ").status == Status.NOT_FOUND
    proof = compare_alcohol("45% Alc./Vol.", "90 PROOF")
    assert proof.status == Status.MATCH_WITH_NOTE and "45" in proof.note
    assert compare_alcohol("45% Alc./Vol.", "40% Alc./Vol.").status == Status.MISMATCH
    assert compare_alcohol("forty-five percent", "forty-five percent").status == Status.MATCH
    assert compare_net_contents("750 mL", None).status == Status.NOT_FOUND
    assert compare_net_contents("1000 mL", "1 L").status == Status.MATCH_WITH_NOTE
    assert compare_net_contents("750 mL", "700 mL").status == Status.MISMATCH
    return 9


def _t_warning() -> int:
    sample = REQUIRED_WARNING
    assert compare_warning(None, None).status == Status.NOT_FOUND
    assert compare_warning(sample, True).status == Status.MATCH
    titled = sample.replace(REQUIRED_WARNING_PREFIX, "Government Warning:")
    title_result = compare_warning(titled, True)
    assert title_result.status == Status.MISMATCH and "capital letters" in title_result.note
    assert compare_warning(sample.replace("WARNING:", "WARNING", 1), True).status == Status.MISMATCH
    reworded = compare_warning(sample.replace("health problems", "health issues"), True)
    assert reworded.status == Status.MISMATCH and "deviates" in reworded.note
    assert compare_warning(sample.replace(" (2) ", "\n(2) "), True).status == Status.MATCH
    assert compare_warning(sample, False).status == Status.MISMATCH
    assert compare_warning(sample, None).status == Status.NEEDS_REVIEW
    return 10


def _t_pipeline() -> int:
    assert best_window([], "OLD TOM") is None and best_window(["OLD", "TOM"], "") is None
    located = best_window(["KENTUCKY", "OLD", "TOM", "DISTILLERY", "750"], "old tom distillery")
    assert located == "OLD TOM DISTILLERY"
    assert best_window(["RIVERBEND", "GIN"], "OLD TOM DISTILLERY") is None
    assert best_window(["VODKA", "Vodka"], "Vodka") == "Vodka"
    overall, results = verify(SAMPLE_APP, SAMPLE_LABEL_TEXT, True)
    assert overall == "PASS" and len(results) == 5
    assert [r.status for r in results] == [Status.MATCH] * 5
    overall_unknown, _ = verify(SAMPLE_APP, SAMPLE_LABEL_TEXT, None)
    assert overall_unknown == "NEEDS_REVIEW"
    overall_fail, _ = verify(Application(brand="RIVERBEND GIN CO"), SAMPLE_LABEL_TEXT, True)
    assert overall_fail == "FAIL"
    blank_optional, fields = verify(Application(brand="OLD TOM DISTILLERY"), SAMPLE_LABEL_TEXT, True)
    assert blank_optional == "PASS" and len(fields) == 2
    assert overall_verdict([FieldResult("x", Status.MATCH), FieldResult("x", Status.NOT_FOUND)]) == "FAIL"
    assert overall_verdict([FieldResult("x", Status.MATCH_WITH_NOTE)]) == "PASS"
    return 13


def _t_batch() -> int:
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        (base / "ok.txt").write_text(SAMPLE_LABEL_TEXT, encoding="utf-8")
        (base / "bad.txt").write_text(f"RIVERBEND GIN CO\n{REQUIRED_WARNING}", encoding="utf-8")
        manifest = base / "m.csv"
        manifest.write_text(
            "file,brand,warning_bold\n"
            "ok.txt,OLD TOM DISTILLERY,yes\n"
            "bad.txt,OLD TOM DISTILLERY,yes\n"
            "missing.txt,OLD TOM DISTILLERY,yes\n"
            "ok.txt,OLD TOM DISTILLERY,maybe\n"
            "ok.txt,OLD TOM DISTILLERY,YES\n",
            encoding="utf-8",
        )
        rows = parse_manifest(manifest)
        assert len(rows) == 5
        results = [verify_row(row, base) for row in rows]
        assert [r.overall for r in results] == ["PASS", "FAIL", "ERROR", "ERROR", "PASS"]
        assert "warning_bold" in results[3].error
        assert batch_exit_code(results) == 1
        assert batch_exit_code([results[0]]) == 0
        assert batch_exit_code([RowResult("x", "NEEDS_REVIEW", [])]) == 3
        bad = base / "bad.csv"
        bad.write_text("file,nope\na,b\n", encoding="utf-8")
        try:
            parse_manifest(bad)
            raise AssertionError("missing-column manifest must raise")
        except ValueError as exc:
            assert "brand" in str(exc)
    return 8


def _self_test() -> int:
    suites = (_t_parsers, _t_text_tiers, _t_numeric_fields, _t_warning, _t_pipeline, _t_batch)
    total = 0
    try:
        for suite in suites:
            total += suite()
    except AssertionError as exc:
        print(f"self-test FAILED in {suite.__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"self-test: {total} assertions across {len(suites)} suites passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
