#!/usr/bin/env python3
"""Run chunk-prompt variants against stored extracts and keep every output.

The outputs are the product: a variant is judged by reading them, not by a
score. The index this prints is a coarse pointer at where to look first.

    python3 bench/run.py --extract ch_5ac8ae4e… --as diff-heavy   # freeze material
    python3 bench/run.py -n 3                                     # run every variant
    python3 bench/run.py --variant citation --material diff-heavy -n 5
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import pathlib
import re
import shutil
import subprocess
import sys
import time

BENCH = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BENCH.parent))
import chsum  # noqa: E402  — sibling module, not an installed dependency

MATERIAL, VARIANTS, OUT = BENCH / "material", BENCH / "variants", BENCH / "out"

# The rule under test, quoted from `_CHUNK_PROMPT` exactly. A variant file
# replaces this span; a mismatch here means the prompt moved and the bench is
# substituting into text that no longer exists — hence the assert in `build`.
BASELINE_RULE = (
    '- Report outcomes only as recorded ("pytest printed 4 passed"), never as a\n'
    '  judgement ("successfully", "correctly", "works").'
)

# Coarse index only. It finds the words reliably and cannot tell a grounded use
# ("verified `a.md` matched `b.md`") from an ungrounded one ("verified the fix
# works") — that distinction is what the reading is for.
WORDS = re.compile(
    r"\b(successfully|correctly|works|working|verified|properly|as expected|"
    r"confirms?|confirmed|ensures?|ensuring)\b", re.I)


def freeze(ref: str, name: str) -> pathlib.Path:
    """Store one chunk of a real session as material. The chunk with the most
    tool traffic, since that is where outcome claims come from."""
    path = chsum.path_for_ref(ref)
    events = chsum._events_since(path, 0, "", "")
    chunks = chsum._chunk_events(events, [], chsum._CHUNK_MAX_CHARS)
    if not chunks:
        raise SystemExit(f"{ref} produced no chunks")
    pick = max(chunks, key=lambda c: sum(1 for e in c if e.kind in ("ran", "output", "failed")))
    dest = MATERIAL / f"{name}.txt"
    dest.write_text(chsum._chunk_material(pick))
    print(f"{dest.relative_to(BENCH.parent)} — {len(pick)} events, {dest.stat().st_size:,} chars "
          f"from {ref}")
    return dest


def build(variant: pathlib.Path) -> str:
    """The full system prompt for one variant: `_CHUNK_PROMPT` with the outcome
    rule swapped for this variant's text. `baseline` leaves it untouched."""
    prompt = chsum._CHUNK_PROMPT
    assert prompt.count(BASELINE_RULE) == 1, (
        "BASELINE_RULE no longer appears in _CHUNK_PROMPT — update bench/run.py")
    if variant.stem == "baseline":
        return prompt
    return prompt.replace(BASELINE_RULE, variant.read_text().strip())


def call(system: str, material: str) -> str:
    """One `claude -p`, the same flags and cwd `HaikuSummariser.digest` uses —
    an extra flag here would price and prompt differently from the real path."""
    exe = shutil.which("claude")
    if not exe:
        raise SystemExit("claude CLI not on PATH")
    chsum.CHSUM_DIR.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [exe, "-p", "--model", chsum.HaikuSummariser.model, "--output-format", "json",
         "--system-prompt", system, "--tools", "", "--setting-sources", ""],
        input=material, capture_output=True, text=True,
        timeout=chsum._CALL_TIMEOUT, cwd=chsum.CHSUM_DIR)
    if proc.returncode != 0 or not proc.stdout.strip():
        return f"[call failed: exit {proc.returncode}]\n{(proc.stderr or proc.stdout)[:400]}"
    try:
        return json.loads(proc.stdout).get("result", proc.stdout)
    except json.JSONDecodeError:
        return proc.stdout


# A cited clock time is the one claim in a bullet that can be checked without
# reading: either the extract stamped that second or it did not.
STAMP = re.compile(r"\[(\d\d:\d\d:\d\d)\]")


def index(text: str) -> tuple[int, int]:
    bullets = [l for l in text.splitlines() if l.lstrip().startswith(("-", "*"))]
    return len(bullets), len(WORDS.findall(text))


def citations(text: str, material: str) -> tuple[int, list[str]]:
    """(cited, invented) — clock times the output carries that the material does
    not stamp. A variant that cites is only worth more than one that does not
    while this stays empty."""
    stamped = set(STAMP.findall(material))
    cited = STAMP.findall(text)
    return len(cited), [t for t in cited if t not in stamped]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--extract", metavar="REF", help="freeze a chunk of this session as material")
    ap.add_argument("--as", dest="as_name", metavar="NAME", help="name for --extract's material")
    ap.add_argument("--variant", action="append", metavar="NAME",
                    help="run only this variant (repeatable; default: all)")
    ap.add_argument("--material", action="append", metavar="NAME",
                    help="run only this material (repeatable; default: all)")
    ap.add_argument("-n", type=int, default=3, metavar="N", help="samples per pair (default 3)")
    ap.add_argument("--run-id", metavar="ID", help="output directory name (default: a timestamp)")
    args = ap.parse_args()

    if args.extract:
        freeze(args.extract, args.as_name or args.extract[:11])
        return 0

    def pick(d: pathlib.Path, chosen):
        files = sorted(f for f in d.glob("*.txt"))
        if chosen:
            files = [f for f in files if f.stem in chosen]
            missing = set(chosen) - {f.stem for f in files}
            if missing:
                raise SystemExit(f"no such {d.name}: {', '.join(sorted(missing))}")
        if not files:
            raise SystemExit(f"nothing in {d.relative_to(BENCH.parent)}")
        return files

    variants, materials = pick(VARIANTS, args.variant), pick(MATERIAL, args.material)
    run_id = args.run_id or time.strftime("%Y%m%d-%H%M%S")
    root = OUT / run_id
    print(f"{len(variants)} variants × {len(materials)} materials × {args.n} "
          f"→ {root.relative_to(BENCH.parent)}\n")

    jobs = [(v, m, i) for v in variants for m in materials for i in range(1, args.n + 1)]
    prompts = {v.stem: build(v) for v in variants}
    texts = {m.stem: m.read_text() for m in materials}

    def one(job):
        v, m, i = job
        return job, call(prompts[v.stem], texts[m.stem])

    results: dict[tuple[str, str], list[str]] = {}
    with cf.ThreadPoolExecutor(max_workers=chsum._CHUNK_WORKERS) as ex:
        for (v, m, i), text in ex.map(one, jobs):
            dest = root / v.stem
            dest.mkdir(parents=True, exist_ok=True)
            (dest / f"{m.stem}.{i}.md").write_text(text)
            results.setdefault((v.stem, m.stem), []).append(text)

    (root / "prompts").mkdir(exist_ok=True)
    for name, text in prompts.items():
        (root / "prompts" / f"{name}.txt").write_text(text)

    print(f"{'variant':22s} {'material':16s} {'bullets':>8s} {'flagged':>8s} "
          f"{'cited':>6s} {'invented':>9s}")
    for (v, m), texts_out in sorted(results.items()):
        b = sum(index(t)[0] for t in texts_out)
        w = sum(index(t)[1] for t in texts_out)
        pairs = [citations(t, texts[m]) for t in texts_out]
        c = sum(n for n, _ in pairs)
        bad = [t for _, ts in pairs for t in ts]
        print(f"{v:22s} {m:16s} {b:8d} {w:8d} {c:6d} {len(bad):9d}")
        for t in sorted(set(bad)):
            print(f"{'':40s}   invented [{t}] — not stamped in {m}")
    print(f"\nRead them: {root.relative_to(BENCH.parent)}/<variant>/<material>.<n>.md")
    print("The counts point at where to look. Whether a flagged word is grounded "
          "is not something they can tell you.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
