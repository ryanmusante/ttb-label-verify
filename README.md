# TTB Label Verify

Offline pre-check for TTB COLA label review: compares filed application data against the transcribed text of a label and renders a tiered, auditable verdict. One file, Python standard library only — no packages, no system tools, no network, no server, no keys.

Version 3.2.0 (2026-06-09).

## Contents

- [Setup](#setup)
- [Run](#run)
- [Web prototype and publishing](#web-prototype-and-publishing)
- [Approach](#approach)
- [Tools used](#tools-used)
- [Test labels](#test-labels)
- [Requirement traceability](#requirement-traceability)
- [Deliverables](#deliverables)
- [Assumptions](#assumptions)
- [Trade-offs and limitations](#trade-offs-and-limitations)
- [Changelog](#changelog)

## Setup

There is none. Any Python 3.12+ interpreter runs it as-is:

```
python3 ttb_verify.py --self-test     # 60 built-in assertions, exits 0
```

## Run

Single label — label text from a file or stdin, application data as flags:

```
python3 ttb_verify.py \
    --brand "OLD TOM DISTILLERY" \
    --class-type "Kentucky Straight Bourbon Whiskey" \
    --alcohol "45% Alc./Vol. (90 Proof)" \
    --net "750 mL" \
    --warning-bold yes \
    sample_labels/old-tom-pass.txt
```

Batch — a CSV manifest maps each label text file to its application data (Chen's 200-300-application importer drops):

```
python3 ttb_verify.py --batch sample_labels/manifest.csv
```

Machine output and scripting:

```
python3 ttb_verify.py --json --brand "OLD TOM DISTILLERY" --warning-bold yes sample_labels/old-tom-pass.txt
python3 ttb_verify.py --help
```

Exit codes: `0` PASS, `1` FAIL, `2` usage/input error, `3` NEEDS_REVIEW. In batch mode the worst row wins: any ERROR/FAIL → 1, else any NEEDS_REVIEW → 3, else 0. Results go to stdout, diagnostics to stderr; color only on a TTY and `NO_COLOR` is respected.

Manifest columns: `file`, `brand` (required); `class_type`, `alcohol`, `net`, `warning_bold` (yes/no/unknown, case-insensitive) optional. Label paths are relative to the manifest. A bad row (missing file, blank brand, invalid flag) becomes a per-row ERROR instead of sinking the batch.

## Web prototype and publishing

`index.html` is a thin browser shell around the unmodified `ttb_verify.py`: it loads
the Pyodide runtime (CPython compiled to WebAssembly, pinned to 314.0.0 / Python 3.14)
from the jsDelivr CDN, executes this repository's Python file as-is, and calls
`verify()` directly. One rules engine, two front ends — the CLI and the page can
never disagree. Label text never leaves the browser tab; nothing is uploaded or
stored, and the page works offline once loaded. First visit downloads the runtime
(roughly 10 MB, cached afterward); each verdict after that is instant. The sample
buttons load the fixtures from `sample_labels/`, and the footer button runs the
same 60-assertion self-test in the browser.

Run it locally (any static file server works; opening the file directly will not,
because browsers block `fetch` on `file://`):

```
python3 -m http.server 8000
# then open http://localhost:8000/
```

Publish both deliverable URLs from this folder (requires a GitHub account and
`git`; `gh` is the GitHub CLI):

```
git init && git add -A && git commit -m "feat: TTB label verify v3.2.0"
gh repo create ttb-label-verify --public --source=. --push
git tag -a v3.2.0 -m "v3.2.0" && git push --tags
```

Then in the repository: Settings → Pages → Deploy from a branch → `main`, `/ (root)`.
After that:

- Source code repository: `https://github.com/<your-username>/ttb-label-verify`
- Deployed application:   `https://<your-username>.github.io/ttb-label-verify/`

Any static host (Vercel, Netlify, an internal IIS box) serves the same folder
unchanged — there is no build step and no server-side code. If the host or a
firewall cannot reach the jsDelivr CDN, download a Pyodide release into the
repository and point `PYODIDE_VERSION`'s URL in `index.html` at the local copy.

## Approach

**Transcribe and adjudicate are separate concerns.** The agent (or any OCR of the operator's choosing) supplies what is printed on the label as plain text; every pass/fail decision is made by deterministic rules in `ttb_verify.py`. Same inputs, same verdict, every time — an auditor can read one file and know exactly why a label passed or failed.

**Verdicts are tiered, not binary** (Morrison's "STONE'S THROW vs Stone's Throw" point). Per field: `MATCH`, `MATCH_WITH_NOTE` (same text, only capitalization/punctuation differs — passes with a visible note), `NEEDS_REVIEW` (≥85% similar; agent judgment), `MISMATCH`, `NOT_FOUND`. Overall: FAIL if anything mismatches or is missing, NEEDS_REVIEW if anything needs eyes, else PASS.

**The government warning is statutory** (Park's checks). The exact 27 CFR Part 16 text is hardcoded; the body must match word-for-word (case-insensitive, whitespace-collapsed, with a pointer to the first deviating words). The `GOVERNMENT WARNING:` header must be exactly all-caps — title case is a hard MISMATCH. Bold type cannot be carried by plain text, so the operator attests it with `--warning-bold`; `unknown` resolves to NEEDS_REVIEW, never a silent pass.

**Fields are located, not assumed.** Free-form fields (brand, class/type) are found by sliding a word window over the label text and keeping the most similar span, case preserved (floor 0.55, below which the field is NOT_FOUND). Alcohol is the first line that parses as % or proof (45% ≡ 90 Proof, hyphenated proof accepted); net contents is unit-aware and normalized to mL (ml/cl/L/fl oz, 1% tolerance).

**Latency** (Chen's 5-second ceiling): everything is local and instantaneous — there is nothing to wait on.

## Tools used

Python 3.12 standard library only: `argparse`, `csv`, `json`, `re`, `difflib.SequenceMatcher`, `dataclasses`, `enum`, `pathlib`, `sys`, `os` (and `tempfile` inside the self-test). Linted with ruff, type-checked with ty; neither is needed to run.

## Test labels

`sample_labels/` contains fixtures generated from the script's own statutory constants, plus a manifest exercising every verdict path:

```
╔══════════════════════════╦═══════════════╦══════════════════════════════╗
║ FIXTURE                  ║ EXPECTED      ║ DEMONSTRATES                 ║
╠══════════════════════════╬═══════════════╬══════════════════════════════╣
║ old-tom-pass.txt         ║ PASS          ║ spec sample, all fields      ║
║ case-only-brand.txt      ║ PASS (notes)  ║ case-only brand; 86 Proof    ║
║                          ║               ║ vs 43%; 75 cl vs 750 mL      ║
║ title-case-warning.txt   ║ FAIL          ║ "Government Warning:" header ║
║ wrong-abv.txt            ║ FAIL          ║ filed 45%, label 40%         ║
║ reworded-warning.txt     ║ FAIL          ║ "health issues" deviation    ║
║ missing-warning.txt      ║ FAIL          ║ warning absent               ║
║ manifest row 7           ║ NEEDS_REVIEW  ║ warning_bold=unknown         ║
╚══════════════════════════╩═══════════════╩══════════════════════════════╝
```

## Requirement traceability

```
╔══════════════════════════════╦══════════════════════════════╗
║ STAKEHOLDER ASK              ║ IMPLEMENTATION               ║
╠══════════════════════════════╬══════════════════════════════╣
║ Match brand, ABV, warning    ║ tiered engine; warning is    ║
║ (Chen: core task)            ║ always checked               ║
╠══════════════════════════════╬══════════════════════════════╣
║ ~5 s per label hard ceiling  ║ local stdlib execution;      ║
║ (Chen)                       ║ effectively instant          ║
╠══════════════════════════════╬══════════════════════════════╣
║ Batch 200-300 applications   ║ --batch manifest.csv with    ║
║ (Chen)                       ║ per-row errors and summary   ║
╠══════════════════════════════╬══════════════════════════════╣
║ Judgment, not rigid match    ║ MATCH_WITH_NOTE for case-    ║
║ (Morrison)                   ║ only diffs; NEEDS_REVIEW     ║
║                              ║ tier at 0.85 similarity      ║
╠══════════════════════════════╬══════════════════════════════╣
║ Warning word-for-word, ALL   ║ statutory text hardcoded;    ║
║ CAPS + bold (Park)           ║ exact-case header; bold      ║
║                              ║ attested, unknown→REVIEW     ║
╠══════════════════════════════╬══════════════════════════════╣
║ Locked-down network          ║ zero network on any path;    ║
║ (Williams)                   ║ runs air-gapped              ║
╠══════════════════════════════╬══════════════════════════════╣
║ No sensitive storage         ║ stateless; reads text,       ║
║ (Williams)                   ║ prints verdict, stores nada  ║
╠══════════════════════════════╬══════════════════════════════╣
║ Simple for all comfort       ║ one command, plain-language  ║
║ levels (Chen)                ║ notes, --help is the manual  ║
╚══════════════════════════════╩══════════════════════════════╝
```

## Deliverables

How the spec's two deliverables are satisfied by this archive:

```
╔══════════════════════════════╦══════════════════════════════╗
║ SPEC DELIVERABLE             ║ WHERE                        ║
╠══════════════════════════════╬══════════════════════════════╣
║ Source code repository —     ║ this archive is the repo     ║
║ all source code              ║ content, git-init-ready:     ║
║                              ║ ttb_verify.py (rules engine) ║
║                              ║ + index.html (web shell)     ║
║                              ║ + sample_labels/ fixtures;   ║
║                              ║ publish commands in Web      ║
║                              ║ prototype and publishing     ║
╠══════════════════════════════╬══════════════════════════════╣
║ README: setup and run        ║ Setup, Run                   ║
║ instructions                 ║                              ║
╠══════════════════════════════╬══════════════════════════════╣
║ README: approach, tools      ║ Approach; Tools used;        ║
║ used, assumptions made       ║ Assumptions                  ║
╠══════════════════════════════╬══════════════════════════════╣
║ Deployed application URL     ║ index.html — static page     ║
║                              ║ running the same Python      ║
║                              ║ file; publish steps in Web   ║
║                              ║ prototype and publishing     ║
╚══════════════════════════════╩══════════════════════════════╝
```

## Assumptions

Application data is entered via flags or manifest CSV — no COLA integration in scope. Label input is transcribed text (one label per file); transcription fidelity is the operator's responsibility. Field set follows the spec's sample-label fields (brand, class/type, alcohol, net contents, government warning); bottler name/address and country of origin are out of scope for the prototype. English labels. US proof is exactly 2× ABV. Net contents tolerance is 1% relative (0.5 mL minimum). The warning statement runs from "GOVERNMENT WARNING" to the end of the label text.

## Trade-offs and limitations

This prototype was deliberately reduced to a dependency-free rules engine in a single Python file plus one static page: no web server, no packaging, no image pipeline. Consequences, stated plainly: the spec's **deployed-URL deliverable is met by a static page** (`index.html`) that runs the same `ttb_verify.py` in the browser via Pyodide — no server-side code, so the only hosting requirement is a static file host; `--self-test` remains the acceptance check on both front ends. The page's single external dependency is the Pyodide CDN at first load (self-hostable, as noted above). **Bold detection is operator-attested**, not measured, because text cannot carry type weight; the conservative default (`unknown` → NEEDS_REVIEW) means it can never silently pass. **Image handling (angles, glare — Park's stretch goal) is out of scope**: any OCR step happens before this tool, and bad transcription shows up as NOT_FOUND/NEEDS_REVIEW rather than being silently mis-verified. The 0.55 locator floor and 0.85 fuzzy-review threshold are sensible defaults, untuned against real COLA data. Verdicts assist agents and do not replace agent judgment.

## Changelog

3.2.0 (2026-06-09)
  - index.html: browser prototype satisfying the deployed-URL deliverable —
    loads the unmodified ttb_verify.py under Pyodide (pinned 314.0.0,
    CPython 3.14) and calls verify() in-tab; sample-label loader buttons;
    in-browser 60-assertion self-test; no data leaves the page.
  - README: Web prototype and publishing section with local-serve and
    GitHub/Pages publish commands; Deliverables and Trade-offs updated —
    deployed URL is now in scope.

3.1.2 (2026-06-09)
  - README: Deliverables section mapping the spec's deliverables to the
    archive; field-set scope pinned to the spec's sample-label fields
    (bottler name/address and country of origin explicitly out of scope).
  - No code changes; version constant bumped to keep the artifact in sync.

3.1.1 (2026-06-09)
  - best_window: on equal similarity, prefer the case-exact candidate over
    the first-seen window (removes spurious case-only notes; +1 self-test).
  - verify_row: manifest warning_bold values accepted case-insensitively,
    matching the existing header handling (+1 batch self-test row).
  - main: bold-flag mapping deduplicated through _BOLD_FLAGS.
  - README: table of contents added; stakeholder names unified to surnames;
    net-contents tolerance documented as 1% relative with a 0.5 mL minimum.

3.1.0 (2026-06-09)
  - Baseline of this changelog.
