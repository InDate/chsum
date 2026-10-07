"""The `chsum observe` scenario: an equivocation check over two text files.

`corpus/glossary.txt` sets one sense for "bank" and "charge";
`corpus/field.txt` is 600 lines of field notes, long enough that the base
clears the model's minimum cacheable prefix. `skill/rules.md` is the base's
system prompt.

    python3 bench/observe/run.py           # scripted run, prints one row per step
    python3 bench/observe/run.py --live    # an observer on this repo, for a session to drive
    python3 bench/observe/run.py --stop    # drops the live observer and its work folder

The scripted run copies the corpus into a temporary directory that is no git
repository, and points CHSUM_DIR at a temporary data directory, so no observer it builds is seen by
any session's hooks; it ends by stopping the observer. Expected: the lender
sentence returns EDIT naming "bank", the river and capacitor sentences return
PASS, the out-of-band edit moves the ref, and every request after the create
reads the base's prefix from cache.

The live observer watches `bench/observe/work/notes/` in this repo, so a
session here reviews its Write and Edit calls, denies its Bash writes, and
shows it on the status line.
"""
import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO))

MODEL = "opus"
NAME = "observe-bench"
WORK = HERE / "work"


def chsum(*args: str) -> None:
    subprocess.run([sys.executable, str(REPO / "chsum.py"), "observe", *args], cwd=REPO, check=True)


def scripted() -> int:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="observe-bench-"))
    repo = tmp / "repo"
    shutil.copytree(HERE / "corpus", repo / "notes")
    os.environ["CHSUM_DIR"] = str(tmp / "data")
    from chsum import observe

    chsum("start", "--root", str(repo), "--name", NAME, "--skill", str(HERE / "skill"),
          "--match", r"notes/.*\.txt", "--model", MODEL)
    field = repo / "notes/field.txt"
    state, rel = observe._state_for(field)
    pings = observe.core.PINGS_DIR / f"{state['sid']}.jsonl"

    def review(label: str, line: str) -> None:
        outcome, detail = observe._review(state, rel, field.read_text() + line + "\n")
        u = json.loads(pings.read_text().strip().splitlines()[-1])
        print(f"{label:<26} {outcome.upper():<5} read {u['read']:>6} wrote {u['wrote']:>5} | "
              f"{' '.join(detail.split())[:110]}")

    review("lender sense of bank", "Day 41, reading 1: the bank approved the loan for the new pump.")
    review("river sense of bank", "Day 41, reading 1: the north bank lost more soil after the storm.")

    held = state["held"]
    field.write_text(field.read_text() + "Day 41, reading 2: the gauge read steady.\n")
    observe.on_hook({})
    for _ in range(60):
        time.sleep(2)
        state = observe._state_for(field)[0]
        if state["held"] != held:
            break
    last = observe._activity(state).get("last") or {}
    print(f"{'out-of-band edit':<26} {'MOVED' if state['held'] != held else 'STUCK':<5} "
          f"read {last.get('read', 0):>6} wrote {last.get('wrote', 0):>5} | {last.get('doing')}")

    review("electrical sense of charge", "Day 41, reading 3: the capacitor dropped its charge to zero.")
    chsum("stop", "--root", str(repo), "--name", NAME)
    shutil.rmtree(tmp, ignore_errors=True)
    return 0


def live() -> int:
    if WORK.exists():
        raise SystemExit(f"{WORK} exists; `--stop` drops the observer and the folder first")
    shutil.copytree(HERE / "corpus", WORK / "notes")
    chsum("start", "--root", str(REPO), "--name", NAME, "--skill", str(HERE / "skill"),
          "--match", r"bench/observe/work/notes/.*\.txt", "--model", MODEL)
    print(f"watching {WORK / 'notes'}: an Edit there adding 'the bank approved the loan' "
          f"is denied, one adding 'the south bank slumped' lands and advances the base")
    return 0


def stop() -> int:
    try:
        chsum("stop", "--root", str(REPO), "--name", NAME)
    finally:
        shutil.rmtree(WORK, ignore_errors=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true",
                      help="start an observer on this repo for a session to drive")
    mode.add_argument("--stop", action="store_true",
                      help="drop the live observer and its work folder")
    args = ap.parse_args()
    return live() if args.live else stop() if args.stop else scripted()


if __name__ == "__main__":
    sys.exit(main())
