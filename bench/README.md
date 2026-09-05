# bench — trying a chunk-prompt variant

`_CHUNK_PROMPT` is the one place chsum asks a model for prose. This runs
candidate wordings against frozen extracts and keeps every output, so a run
today and a run next week compare.

The run prints `cited` and `invented` alongside the word counts. A cited
clock time is the one thing in a bullet checkable without reading: the extract
either stamped that second or it did not. `invented` above zero disqualifies a
citing variant outright.

**The outputs are the product. You read them.** The counts printed at the end
find the words reliably and cannot tell a grounded use — `verified `a.md`
matched `b.md`` — from an ungrounded one — `verified the fix works`. That
distinction is the reason to open the files.

```sh
python3 bench/run.py -n 3                                # every variant × every material
python3 bench/run.py --variant citation --material diff-heavy -n 5
python3 bench/run.py --extract ch_5ac8ae4e… --as api-heavy   # freeze new material
```

## Layout

```
bench/
  material/<name>.txt        one chunk's extract, frozen and committed
  variants/<name>.txt        the outcome rule this variant substitutes in
  variants/baseline.txt      the rule `_CHUNK_PROMPT` ships with, verbatim
  out/<run-id>/<variant>/<material>.<n>.md    every output, kept
  out/<run-id>/prompts/<variant>.txt          the exact system prompt sent
```

The full prompt is written beside each run's outputs, so a result stays
readable after `_CHUNK_PROMPT` moves on. `out/` is gitignored — the material
and variants are what make two runs comparable, and committing thirty
generated files per run would not.

## Adding a variant

Drop a file in `variants/`. It replaces one span of `_CHUNK_PROMPT` —
`BASELINE_RULE` in `run.py`, quoted from the prompt exactly. If that span stops
matching, `run.py` asserts rather than substituting into text that no longer
exists.

## Material

Frozen from real sessions: `--extract` takes the chunk with the most `ran` /
`output` / `failed` events, since that is where outcome claims come from.
Committed, so two runs see identical input — a variant that looks better
against fresh material may only have drawn an easier chunk.

## What is known

Measured 2026-08-27, one window (`ch_5ac8ae4e…` turns 1–12), two runs each
through the full `recap` path:

| rule | flagged words | bullets |
|---|---|---|
| `baseline` (banned-word list) | 6 | 97 |
| `positive` (list removed) | 16 | 104 |

Removing the banned-word list roughly doubled the rate, against both
Anthropic's published advice (*"tell the AI what TO do instead of what NOT to
do"*) and this repo's own note that a prohibition can anchor toward what it
bans. The advice may still be right in general; it did not hold here.

A six-variant harness on one chunk produced no winner — 3 to 9 hits per arm
across ~24 bullets is noise, and the arms disagreed with the pipeline result.

Three regex metrics were tried and two measured the wrong thing. The last one
classified 3 of 4 known cases correctly and missed the one that started this:
`verified the fix works by running `chsum.py recap`` passes an
evidence-present check, because the backticks cite the command rather than the
outcome. Whether a claim is grounded depends on what the evidence is evidence
*of*, which is why the counts here are an index and not a verdict.

## First pass, 2026-08-27

5 variants x 2 materials x 3 samples, `out/first-pass`:

| variant | bullets | flagged | cited | invented |
|---|---|---|---|---|
| abstain | 36 | 7 | 0 | 0 |
| baseline | 48 | 13 | 0 | 0 |
| citation | 40 | 15 | 32 | 0 |
| positive | 42 | 10 | 0 | 0 |
| transcribe | 35 | 10 | 0 | 0 |

The counts and the reading disagree, and this is the case the counts exist to
be doubted on. Nearly every flagged bullet in every variant reads
"verified that two markdown files were identical" — and the extract contains
the string `IDENTICAL`, so that is a report. The defect appears rarely, as an
editorial tail: "confirming a file transformation was working correctly",
"caching was functioning properly", "indicating consistent output between runs".

`citation` carries the most flagged words and reads best, because each use
arrives attached to its evidence:

```
- Two test output files were verified to be identical through a diff comparison [17:23:47] "IDENTICAL".
- ...tested with `--dry-run` and confirmed it would report "2 calls would be made" [17:24:18].
```

32 of 32 emitted timestamps appear verbatim in the material given to that call.
An earlier design carried `HH:MM` through the summariser and was deleted; there
the copied time *placed* the bullet, so a wrong copy misplaced it silently. Here it attributes, and a wrong copy is what `invented`
counts.

### The reading, which overturned the counts

`baseline` won. It is the most concrete of the five — "2 API calls would be
made (10,687 chars, ~2.7k tokens)", "5 cache hits, 1 miss" — keeps "Claude did
X" throughout, and carried no editorial tail in the sample read.

`citation` scored best on groundedness and reads worst. Its wording pushes the
model into passive voice — "Two test output files were verified", "Testing
revealed", "A script execution showed" — which discards the actor attribution
`_CHUNK_PROMPT` was fixed to produce. Across six samples, 16 of 40 citation
bullets name Claude against baseline's 39 of 48; two citation samples name it in
none. One bullet welded a diff result onto a cache-metrics event.

`abstain` carried the worst editorial tail in the set ("confirming a file
transformation was working correctly", "efficient cache usage"): permission to
abstain produced more assessment, not less.

Every variant written to fix one defect introduced a worse one, and the defect
that motivated the work appeared once, in one live run. Four layers of counting
pointed at `citation`; five minutes of reading overturned it. **Read the
outputs. The table is where to start, not what to conclude.**
