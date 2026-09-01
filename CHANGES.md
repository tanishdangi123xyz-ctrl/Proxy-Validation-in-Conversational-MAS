# Changes

Every change to this repository that could affect a measurement, why it was
made, and how to tell whether it is the cause of something you are looking at.

**How to use this file when something looks wrong.** Find the `config_hash` on
the rows you are suspicious of, look it up in [Hash history](#hash-history), and
read every entry from that hash forward. Anything listed there is a difference
between the code that wrote those rows and the code you are reading now.

**Rules this file exists to serve.** Runs with different `config_hash` or
`prompts_hash` are not comparable and must not be pooled. Nothing derived is
stored. Grading policy is frozen before results are seen. A change that touches
grading after data exists is a change that has to be argued for in writing,
which is what the entries below are.

---

## Hash history

| config_hash | prompts_hash | Period | What it means |
|---|---|---|---|
| `175b1824b6128ca7` | N/A | to 2026-08-21 | Pre-`gsm-hard` config. `dry_20260821T124335Z` only. Not comparable to anything later. |
| `7edf25058c9d34bb` | N/A | 2026-08-21 to 08-22 | Stage-1 screening runs (`data/screen/screen_*`). |
| `fcf5f684b0438ca3` | `a80170cf67250404` | 2026-08-22 to 08-23 | `gsm_hard` + `hotpotqa` frozen. Dry runs `045458Z`, `050556Z`. **Grading defects live, see 2026-08-23 entries.** |
| `295678eeb8606cb8` | `a80170cf67250404` | 2026-08-23 | Samplers pinned, grading repaired, topologies repaired. Dry runs `054825Z`, `055334Z`. |
| `c704ed501758a150` | `a80170cf67250404` | 2026-08-23, Phase 1.3 only | Adds `THERMAL_POLL_S` and `SETTLE_POLL_S`. No behavioural difference to any call. |
| `74b943de71fe5fd8` | `a80170cf67250404` | 2026-08-23 to 08-24 | Adds `TRIGGER_CHIP`, `TRIGGER_LINE`, `TRIGGER_CONSUMER`, `METER_POLL_S`, `METER_RATE_FLOOR_HZ`, `HW_FAULT_ALERT_EVERY`. No behavioural difference to any call, but the hash moved, so rows either side must not be pooled. Dry runs `130219Z`, `131604Z`, `20260824T121313Z`. |
| `74b943de71fe5fd8` | `61e6c78bad256b91` | 2026-08-24 onward | **Current, verified.** `config.py` unchanged from the row above. `debate_agent.txt`'s reconsideration paragraph rewritten to require evidence-grounded engagement with a peer's answer, to address `hotpotqa`'s debate answer-change rate sitting on the null-topology floor. Confirmed by dry run: `hotpotqa` change rate 10.0% -> 17.5% at n=40 agent-rounds, `hotpotqa` debate accuracy 60%, in band. See the 2026-08-24 verification entry. |

Item files are unchanged throughout and hash-verified on every load:
`gsm_hard` `042ee4fe905b2304`, `hotpotqa` `b036fe9bbe7fc79a`.

### Where these changes live

| Section below | Commit |
|---|---|
| Grading, sampling and topology repair | `8795032` |
| Phase 1.1, the validate guard | working tree, uncommitted |
| Phase 1.2, campaign entry point | working tree, uncommitted |
| Phase 1.3, thermal gating | working tree, uncommitted |
| Post-1.3 de-risking | working tree, uncommitted |
| Phase 1.4, the three hardware interfaces | working tree, uncommitted |
| Post-131604Z, topology-items visibility and truncated-call capture | working tree, uncommitted |
| README rewrite and full-repo documentation pass | working tree, uncommitted (docs only) |
| README style cleanup and setup/layout additions | working tree, uncommitted (docs only) |
| README section reorder (file structure and setup promoted ahead of the detail section) | working tree, uncommitted (docs only) |
| `debate_agent.txt` rewrite (evidence-grounded reconsideration, targeting the `hotpotqa` change-rate floor) | working tree, uncommitted |
| Mac-side memory investigation (frozen config's real footprint, ~4.2 GB) | working tree, uncommitted (docs only) |
| `dry_run.py` planner_worker plan-fallback check, stale cross-reference fixes | working tree, uncommitted |

`8795032` is the commit that moved `config_hash` to `295678eeb8606cb8`. Anything
recorded before it carries a different hash and must not be pooled with anything
recorded after.

---

## 2026-08-26: Verified: `planner_worker` never falls back, Stage 2 (recheck the agentic environment) closed

Evidence, not a code change. `--items 40 --topology-items 20` against a
live server, `config_hash 74b943de71fe5fd8` unchanged, 748 call records,
same shape as every other dry run this session.

**The new check, first result.** `gsm_hard`: plan produced on 20 of 20
items. `hotpotqa`: plan produced on 20 of 20 items. Zero fallbacks on
either dataset, at the same `--topology-items 20` sample size the debate
and solver_critic checks were themselves first read at. The planner is
reliably emitting `PLANNER_WORKER_SUBTASKS` well-formed subtasks; the
raw-task fallback `planner_worker.py`'s docstring warns about is a real
code path but not one this model is actually triggering under the frozen
prompts and sampler settings.

**Everything else in this run reproduced the prior reading exactly.**
Debate answer-change rate: `gsm_hard` 27.5% (11/40), `hotpotqa` 17.5%
(7/40), identical to the post-fix verification run earlier this session,
same sample, same server, same result, which is itself a small piece of
confidence that the reading is stable rather than a one-off. Solver-critic
revision growth: `gsm_hard` 515 vs 508 tokens, `hotpotqa` 155 vs 148,
both `OK`, unchanged. Format adherence 100% at every temperature, 0/748
retries, 0/748 thinking-tag leaks, 0 prompt truncations, `MAX_TOKENS` hit
on 9/748 calls (1.2%), gold-shape counts unchanged (dataset-level, not
expected to move).

**Stage 2 status.** Every topology now has a dedicated behavioural check,
not just the generic accuracy/calls table, and every one currently passes:
`baseline` needs no check (single call). `debate` clears the >10%
null-topology floor on both datasets. `solver_critic` shows genuine
revision, not flat-accept, on both datasets. `planner_worker` shows zero
raw-task fallback on both datasets. Nothing here is Jetson-specific or
blocked on hardware; this closes the "recheck the agentic environment"
stage on dev hardware, the same scope Stage 1 was closed at.

## 2026-08-26: `dry_run.py` gets a `planner_worker` plan-fallback check, Stage 2 (recheck the agentic environment) starts here

Not a `config_hash`/`prompts_hash`-moving change: report code only, no
`config.py` value and no prompt touched, so this is comparable across
every existing run, retroactively as well as going forward.

**Why this specific check, first.** Going topology by topology before
trusting any of them going into the real campaign: `baseline` is a single
call, nothing to check. `debate` has its own dedicated null-topology check
(section 3, the answer-change-rate floor), just fixed and reverified this
same session. `solver_critic` has its own dedicated check (the
revision-prompt-growth test), resolved 2026-08-24. `planner_worker` had
**no dedicated check at all**, only the generic accuracy/calls table
every topology gets in section 4. That's a real gap against this file's
own stated purpose, its module docstring lists "does every topology
actually do the thing it is named after" as one of the five questions
`dry_run.py` exists to answer.

`planner_worker.py`'s own docstring documents a concrete failure mode
nobody was checking for: if the planner fails to emit
`config.PLANNER_WORKER_SUBTASKS` well-formed numbered subtasks after its
retries, `run()` silently falls back to `subtasks = [task] *
config.PLANNER_WORKER_SUBTASKS`, handing every worker the raw,
undecomposed task instead of a genuine subtask. If that fires often, the
topology quietly degenerates into "N workers redundantly attempting the
whole problem," structurally the same kind of null-topology risk debate
and solver_critic both had and both already got caught for. The code
already computes this, `plan_ok` comes back in every `planner_worker`
result dict, it was just never aggregated or reported.

**What changed.** `scripts/dry_run.py`: `run()` now collects
`planner_plan_ok[dataset]`, one bool per item, whether the planner
produced a usable plan on that item. `report()` gained a new parameter and
a new unnumbered addendum to section 4, printed right after the existing
solver-critic revision-growth check, in the same style: per dataset, how
many of the sampled items got a real plan versus fell back, flagging
`*** PLANNER FELL BACK TO RAW TASK ***` on any shortfall. No threshold
tolerance was applied (unlike debate's >10% floor or the critic's growth
ratio), there is no existing evidence yet to calibrate a tolerance
against; this is a first reading, not a recalibrated one, so any fallback
at all is surfaced rather than silently allowed under an invented cutoff.

Two stale cross-references were also found and fixed while touching this
section of `README.md`'s Navigation entry for `dry_run.py`: "Section 3.7"
and, nearby, "Section 3.3" and "Section 4.2" elsewhere in the file, all
three survivors of the 2026-08-24 section reorder that the reorder's own
remap pass missed because the section number fell on the line *after* the
word "Section" due to word-wrapping, the same class of bug the reorder's
own entry already flagged and partially fixed once; this is evidence that
class of bug can still hide in wrapped prose after a mechanical remap, not
a new instance of the original bug recurring.

**Not yet done.** This has not been run. `python3 scripts/dry_run.py
--items 40 --topology-items 20` against a live server is what would
actually tell us `planner_worker`'s real fallback rate; until that
happens, whether this topology has been quietly degenerating on some
fraction of items is still an open question, exactly the state debate's
answer-change rate was in before its own check existed.

## 2026-08-26: Mac-side memory investigation: the frozen model's real footprint is roughly 4.2 GB, not 13.56 GB

Not a code change, evidence, and not a Jetson result either; everything
here was measured on the laptop, and the entry says explicitly where that
limits what it can claim. `config_hash` and `prompts_hash` both unchanged.

**Where this started.** Activity Monitor showed `llama-server` (this
project's `serve_dev.sh`, frozen flags, native BF16 Qwen3-1.7B, `CTX_SIZE
3072`) using 13.56 GB on a 24 GB Mac. Checklist item 5 ("model download and
throughput calibration") was already flagged as unresolved for the Jetson;
this reading raised a sharper question underneath it that the checklist
item's wording didn't originally cover: does the frozen configuration fit
in 8 GB at all, not just how fast does it run.

**Ground truth from the file itself, no live server needed.** The GGUF
header was read directly (`struct`-parsed, standard library, the
metadata section only, not the 3.4 GB of tensor data after it), rather
than guessed from memory. Confirmed architecture: `qwen3`, 28 layers, 8 KV
heads, 128 dimensions per head, 2048 embedding length. The model file on
disk is 3,447,349,568 bytes (3.45 GB); since `LLAMA_FLAGS` includes
`--no-mmap`, that entire file is read into real memory, not left as
reclaimable page-cache. The KV cache at the frozen `CTX_SIZE=3072`,
`KV_CACHE_TYPE=f16`, `--parallel 1` computes to 2 x 28 x 8 x 128 x 3072 x 2
bytes = 352 MB, using the model's own real dimensions rather than an
assumed architecture. Weights plus KV cache: **3.8 GB**, computed, not
measured.

**One flag worth a caution, not a fix.** `serve_dev.sh`'s own output
prints `DEPRECATED: --mmap and --no-mmap are deprecated. use --load-mode
mmap instead`. Deprecation normally still honours the old flag, so the
"whole file lands in real memory" assumption above is very likely still
correct, but this was not verified against llama.cpp's source for this
specific build, and the flag disappearing in a future `llama.cpp` update
would silently invalidate it. Added to Standing Cautions below.

**What `vmmap -summary` on the live process showed, and why it looked
contradictory at first.** Two snapshots of the same PID (22253), taken a
few minutes apart, one before firing a completion request and one just
after:

| | resident | dirty | swapped | dirty+swapped |
|---|---|---|---|---|
| snapshot 1, idle | 2.0G | 1.6G | 2.6G | 4.2G |
| snapshot 2, after a request | 488.0M | 92.6M | 4.2G | 4.3G |

Resident dropped and swapped rose between the two readings, which looks at
first like the process got colder after being used, backwards from what
firing a request should do. Read correctly, this is macOS's page
compressor working under the system-wide memory pressure the same Mac was
already showing (Activity Monitor's own reading at the time: 24 GB
physical, 20.87 GB used, 11.03 GB swap, system-wide, across everything
running, not just this process). Chasing "hot vs. idle" in that
environment is chasing noise: with a browser, an IDE, and several other
processes all competing for the same 24 GB, the compressor reshuffles
which pages are resident versus swapped from one moment to the next
regardless of what `llama-server` itself is doing.

**The number that survives the noise.** `dirty + swapped`, the memory this
process actually owns and cannot simply drop for free (unlike a clean
file-backed page, which can be discarded and re-read), holds essentially
constant across both snapshots: 4.2 GB and 4.3 GB. Only the split between
"currently resident" and "currently swapped" moved. That stability, from
two readings taken under different momentary pressure, is a real result,
not a coincidence: **the frozen configuration's true memory commitment on
this Mac is approximately 4.2 GB**, close to the 3.8 GB computed
independently from the GGUF header, with the remaining ~0.4 GB plausibly
llama-server's own process overhead and Metal compute buffers, not the
alarming ~9.7 GB gap the raw 13.56 GB Activity Monitor number had implied.

**What this does and does not settle.** It substantially de-risks the
"does this even fit" question: 4.2 GB inside an 8 GB Jetson leaves real
headroom for JetPack, the orchestrator process, and GPIO/thermal handling,
where 13.56 GB flatly would not have. It does not settle the question,
because every number above came from macOS's Metal backend, not Jetson's
CUDA backend, and the ~0.4 GB of backend-specific overhead on top of the
3.8 GB GGUF-derived floor is not guaranteed to be the same size, or even
the same sign, under CUDA. The only way to close this for real is the same
`vmmap`-equivalent measurement (`tegrastats` or `/proc/meminfo`, watched
during an actual call) on the physical device, which is exactly what
`check_device.py` still has no memory check for, see Standing Cautions.

**What this also demonstrates, independent of the number.** Under enough
memory pressure, this Mac's `llama-server` process does get swapped, not
merely slow, actually paged to disk. That is a direct threat to this
project's own measurement validity if it happens mid-call on the Jetson:
swapping during a trigger-bracketed call would corrupt the timing the
energy attribution depends on. Whether the Jetson, run headless with
nothing else competing for its 8 GB, would ever reach that pressure is
unknown and needs its own check, not assumed safe by analogy to a
24 GB machine that had a browser and an IDE open.

## 2026-08-24: Verified: the `debate_agent.txt` fix clears the `hotpotqa` change-rate floor

Direct follow-up to the entry below. A live dry run against `61e6c78bad256b91`
was requested there and has now been run: `python3 scripts/dry_run.py --items
40 --topology-items 20`, 748 call records, `config_hash 74b943de71fe5fd8`
unchanged, run in Tanish's own terminal. Exact run_id not captured in this
entry; if it is still needed, `data/raw/dry_*.calls.csv` on the machine that
ran it, sorted by mtime, has it.

**Item 1, the floor.** Cleared, with real margin this time rather than
sitting on the boundary. `hotpotqa` debate answer-change rate is 17.5% (7
of 40 agent-rounds), up from `121313Z`'s 10.0% (4 of 40) under the identical
`--topology-items 20` methodology, so this is a direct, comparable
before/after on the same metric at the same sample size. `gsm_hard`, which
did not need fixing, held steady: 27.5% (11 of 40) against `121313Z`'s 30%
(12 of 40), a one-agent-round difference that is ordinary sampling noise,
not a regression the prompt rewrite caused.

**Item 2, accuracy.** `hotpotqa` debate accuracy is 60% (12/20), inside the
45-70% target band and close to `hotpotqa` baseline's own 65% at the same
small n. No sign that pushing agents to justify keeping or changing an
answer cost them correct answers. `gsm_hard` debate reads high at 75%
(15/20) against a baseline of roughly 55-60% at these temperatures; this
report does not compute a confidence interval for section 4's per-topology
accuracy the way section 2 does for baseline, so this reads as a
noteworthy but statistically unresolved number, not a confirmed
improvement, at `--topology-items 20`. Not a concern for the fix under
review, since `gsm_hard`'s change rate was already healthy before this
change and the fix's own target was `hotpotqa`.

**Mechanically clean.** Format adherence 100% at every temperature, 0/748
retries, 0/748 thinking-tag leaks, 0 prompts truncated by context. The
`solver_critic` revision-prompt-growth check, unrelated to this change but
a general trip-wire for the pipeline, still passes both datasets
(`gsm_hard` grew 515 tokens against an expected 508, `hotpotqa` 155 against
148), confirming the debate-only prompt edit did not disturb anything
outside `debate.py`'s own topology.

**One honest side effect, not yet investigated.** `MAX_TOKENS` (512) was
hit on 9 of 748 calls (1.2%), up from `121313Z`'s 7 of 748 (0.9%). Context
budget is not the cause: 0 prompts were truncated and 1323 tokens of
headroom remain under `CTX_SIZE` at the worst observed prompt+output
(1749). The new reconsideration paragraph asks an agent to name specific
evidence either way, which plausibly makes some completions run longer;
plausible, not confirmed, since which calls hit the cap and on which
dataset was not read from the raw completions the way the `121313Z` entry
did with `--debug-truncated`. Small enough (2 extra calls) that it does not
change this entry's verdict, but worth a `--debug-truncated` pass if the
rate climbs on a larger run.

**Verdict: fix confirmed at the sample size tested, not yet a permanent
close-out.** Both conditions the entry below set for treating this as
resolved are met: the floor is cleared with margin, and `hotpotqa` debate
accuracy stays in band. This is still the same `--topology-items 20`
sample size as the reading it is being compared against, not more power
than before, so it should be read as "the fix worked at n=40, twice
measured under two different prompt versions" rather than as a
statistically settled result; a larger `--topology-items` pass before the
real campaign locks in would resolve this the same way the baseline
accuracy band still needs more items to resolve. The Standing Caution
below is updated to reflect a verified, not merely applied, fix.

## 2026-08-24: `debate_agent.txt` rewritten to fix the `hotpotqa` answer-change-rate floor

`prompts_hash` moves: `a80170cf67250404` -> `61e6c78bad256b91`. `config_hash`
unchanged (`74b943de71fe5fd8`); nothing in `config.py` touched. **This entry
records a decision and the change made to act on it. It does not record a
verification, because none has happened yet; see "What is still open"
below before treating any `debate`/`hotpotqa` row under the new hash as
validated.**

**The question being closed.** The 2026-08-24 `121313Z` entry above left
open whether `hotpotqa`'s debate answer-change rate sitting exactly on the
>10% null-topology floor (10.0%, confirmed at n=40 agent-rounds, not
small-sample noise) was a genuine near-null finding about this dataset, or
a prompt-engagement problem worth fixing before committing to it in the
real campaign. Decided: treat it as the latter, on request.

**Why this reads as an engagement problem rather than "nothing to
disagree about."** Ruled out first: this is not a repeat of the
`DEBATE_SHOWS_OWN_PRIOR` bug (`config.py`'s comment on that flag, and see
`debate.py`'s own docstring). That bug is about an agent having no memory
of its own prior answer; it is fixed, and has been on since before
`121313Z`. `debate.py` also already passes each peer's *full* raw response,
reasoning included, not just their final line, into the next round's
prompt, so an agent is not being asked to reconsider blind. What is
missing is direction: the old reconsideration paragraph ("if their
reasoning is better than yours, change your answer") does not tell the
model what "better" means or where to look for it. For `gsm_hard`, open-
ended multi-step arithmetic gives two independently-computing agents
natural surface variance to catch, right or wrong, so the vague instruction
still has something to bite on (`gsm_hard` debate change rate is a healthy
30%). For `hotpotqa`, short factual-span extraction from a passage does
not have that variance; once a model has quoted what it believes is the
right entity, a vague "was their reasoning better" invites it to just
restate its own line rather than actually checking the peer's specific
claim, which reads as agreement without engagement, not agreement because
both agents are correctly converging on a genuinely unambiguous answer.

**What changed.** `debate_agent.txt`'s middle paragraph, the only part of
this file the topology's deliberation behaviour turns on, replaced:

> When you are shown other solvers' answers, consider them genuinely: if
> their reasoning is better than yours, change your answer; if you believe
> yours is correct, keep it and say why. Do not simply agree.

with:

> When you are shown another solver's answer, engage with the specific
> fact, quoted detail, or calculation step behind it, not just their final
> line. If their answer differs from yours, find the exact piece of
> evidence or working that supports theirs and check it directly against
> the source material or your own arithmetic. Change your answer only when
> you can point to a specific error in your own prior evidence or working;
> otherwise keep your answer and state exactly which piece of the peer's
> supporting detail is wrong, missing, or insufficient. Do not restate your
> own answer without addressing theirs, and do not adopt theirs without
> checking it first.

Nothing else in the file changed: the working-through-the-problem
instruction and the `Answer:` line format are untouched, so this is
purely a deliberation-behaviour change, not a format or output-schema
change.

**Why this wording, specifically, rather than just telling the model to
change its answer more often.** A prompt that simply pressured agents to
flip more often would raise the change-rate number without raising genuine
deliberation, which is exactly the failure mode `DEBATE_SHOWS_OWN_PRIOR`'s
own docstring warns about: "looks like vigorous disagreement... while no
deliberation is happening." That would be gaming the metric, not fixing
the topology, and this file's own rule is that a change made to move a
number after seeing it has to be argued in writing, not just made. The new
wording is built to raise genuine engagement specifically: it requires an
agent to name a specific fact or step behind a differing answer in either
direction, whether it ends up keeping its own answer or changing it, and it
gates a change on finding a specific error rather than general unease. If
this is working as intended, both `hotpotqa`'s change rate should move and
the reasoning text preceding each `Answer:` line should visibly reference
specific details from the peer's response, not just restate the agent's
own working; the second of those is worth spot-checking by hand on the
next dry run's raw completions, not just reading the aggregate percentage.

**What is still open, and needs a live dry run to close.** This change has
not been run against a live `llama.cpp` server. Two things need checking,
not assumed:

1. Does `hotpotqa`'s debate answer-change rate clear the >10% floor under
   the new wording, at a sample size large enough to trust (`121313Z`'s
   n=40 is the bar already cleared once; `--topology-items 20` reproduces
   it).
2. Does `debate`'s `hotpotqa` accuracy stay inside the target band. A
   wording that makes agents more willing to change an answer under peer
   pressure could, as a side effect, make them abandon correct answers
   more often too; a change-rate fix that quietly costs accuracy is not a
   fix, it just moves which number looks wrong.

Run `python3 scripts/dry_run.py --topology-items 20` (plus `--items 40` to
also keep power on the other sections) against a live server and read
sections 2 and 3 of its report for both. Until that happens, the Standing
Caution above stands: no `debate`/`hotpotqa` row recorded under
`61e6c78bad256b91` should be treated as a validated topology, only as data
collected under a hash that has not yet been checked against its own
purpose.

## 2026-08-24: README section reorder, file structure and setup moved ahead of the detail section

Not a measurement-affecting change: docs only, `config_hash` and
`prompts_hash` are unaffected. A structural follow-up to the entry directly
below, at the user's explicit request: the file structure and setup
subsections added there were promoted from subsections deep inside the
"what exactly the project is doing in detail" section to their own
top-level sections, and moved to come immediately after the Introduction
and Description, ahead of that detail section rather than after it.

- What was `### 3.9 Repository file structure` is now `## 3. Repository
  file structure`, and what was `### 3.12 Setup and running` is now
  `## 4. Setup and running`. Both now sit directly after `## 2.
  Description`, before the reader ever reaches the methodology detail.
- The former `## 3. What exactly the project is doing: in detail` (with
  its subsections `3.1`-`3.8`) shifted down to become `## 5`, with its
  subsections renumbered `5.1`-`5.8`. `### 3.10 What runs where` and
  `### 3.11 Frozen parameters` became `## 6` and `## 7` respectively,
  keeping their position immediately after the detail section rather than
  moving. The old `## 4. Navigation` and `## 5. Changes and fixes` shifted
  down to `## 8` and `## 9`. No section's own content changed, only its
  number and, for two of them, its position in the document.
- Every in-prose cross-reference of the form "Section X.Y" throughout the
  entire file (there are several dozen, `README.md`'s Navigation section
  alone references the detail section by number repeatedly) was remapped
  to the new numbering. Longer compound section numbers (`3.10`, `3.11`,
  `3.12`) were remapped before the shorter ones (`3.1`-`3.9`) to avoid a
  naive replace of `3.1` corrupting `3.10`/`3.11`/`3.12` via a partial
  match. One stale reference, "as established throughout Section 3 above,"
  needed to become "Section 5 below" rather than a straight renumbering,
  since Setup now precedes the detail section instead of following it;
  this one was checked and fixed by hand after the bulk remap, since it
  changes which word ("above"/"below") is correct, not just the number.
  A second bare "Section 3" reference, inside the new Section 3 itself,
  pointing at its own stale-`doc/`-and-`host/` note, was checked and
  confirmed already correct as a self-reference, needing no change.
- Verified after the reorder: every top-level heading and subsection lands
  at the position implied by its new number (`grep -n '^## \|^### '`),
  every "Section X.Y" reference in the file resolves to a real, correctly
  numbered section, the em-dash count is still zero in `README.md`, and
  every section seam (the ten lines spanning each boundary the reorder
  introduced) reads as continuous prose with no orphaned fragments from
  the block reassembly.

**Verified discriminating.** This entry's own claim, that the reorder
introduced no stale numbering, is checked by the two greps above rather
than asserted: a `Section [0-9]+(\.[0-9]+)?` sweep across the whole file
after the edit turned up exactly one reference that still needed
individual attention (the "above"/"below" case), and it was resolved and
re-verified before delivery.

## 2026-08-24: README style cleanup (no em dashes) and setup/layout additions

Not a measurement-affecting change: docs only, `config_hash` and
`prompts_hash` are unaffected. Two follow-up requests against the README
rewritten in the entry below, both purely additive/stylistic per the user's
own instruction that the prose already written should not be changed:

- Every em dash in `README.md` and `CHANGES.md` was replaced with ordinary
  punctuation (a colon for a header or a bold-term definition, a semicolon
  where two independent clauses had been joined, a comma for everything
  else), at the user's request. Handled with a script, then spot-checked by
  hand for comma splices the mechanical pass left behind; the handful found
  were fixed individually. Zero em dashes remain in either file.
- Four new subsections were added to `README.md`, after Section 3.8 and
  before the Navigation section, none of them replacing or editing
  anything already written: **3.9 Repository file structure** (the actual
  current directory tree, confirmed against the repository on disk, not
  copied from any older draft); **3.10 What runs where** and **3.11 Frozen
  parameters** (both reproduced from a table the user supplied as a
  screenshot of an earlier README draft, cross-checked here against
  `config.py`'s actual current values and annotated wherever the screenshot's
  claims no longer match reality, rather than reproduced blindly: `host/`
  does not exist anywhere in this repository, not even as a placeholder,
  despite being named in the original table and in the frozen-parameters
  section's `doc/MAS_Jetson_Design_Analysis.md` reference, and
  `config.NVPMODEL_MODE` is still genuinely unset rather than already
  "fixed" as the old bullet implied); and **3.12 Setup and running**
  (the still-accurate `serve_dev.sh`/`run_campaign.py`/`selftest.py`/
  `diagnose.py` commands from that same older draft, plus a setup checklist
  rewritten to reflect this project's actual current status rather than the
  old draft's checklist, which had left most steps unchecked despite them
  already being done).

## 2026-08-24: README rewritten from scratch; full-repo read-through, two documentation-accuracy findings

Not a measurement-affecting change: no file under `src/masenergy/` or
`scripts/` was touched, `config_hash` and `prompts_hash` are unaffected, and
nothing here changes what any future run records. Logged anyway because the
project's own rule is that every change to any file in this repository is
recorded here, and a README rewrite is a change to a file in this repository.

`README.md` was scrapped and rewritten in full, at the user's explicit
request, structured as: an introduction; a description of what the project
is; an extreme-detail explanation of what the project actually does
(measurement protocol, the trigger-bracketed call, the four topologies,
dataset/grading rules, the accuracy band and its statistics, hardware fault
handling, campaign structure and resumability, the bring-up/verification
philosophy, and current status); a navigation section covering every file in
the repository, every module-level function and every class method,
described individually, plus all 8 frozen prompt files quoted in full; and a
closing pointer to this file as the authoritative change log. The rewrite
required re-reading essentially the entire codebase in full (every file
under `src/masenergy/`, every script under `scripts/`, every prompt file,
`requirements.txt`, `.python-version-note`) directly from source rather than
from memory, specifically because several files (`client.py`, `dry_run.py`,
`selftest.py`, `jetson.py`, `gpio.py`, `ina3221.py`, `check_device.py`) have
been edited multiple times over the course of this project and a summary
carried forward risked describing behavior that no longer matches what is on
disk.

Two accuracy findings surfaced during that read-through, neither acted on;
this entry documents them, it does not fix them, per the same discipline
applied to every other finding in this file:

- **`scripts/verify_fixes.py` is confirmed broken, by actually running it.**
  `python3 scripts/verify_fixes.py` raises `AttributeError: module
  'masenergy.datasets' has no attribute 'GSM8K'` at import time, because the
  module-level line `GSM8K, HOTPOTQA = ds.GSM8K, ds.HOTPOTQA` predates the
  rename from `gsm8k` to `gsm_hard` and `datasets.py` no longer exports
  `GSM8K`. Every case this file's `EXTRACTION`/`GRADING` fixtures cover is
  already duplicated and updated inside `scripts/selftest.py`'s
  `test_extraction()`/`test_grading()`, which do run and do pass. Left as a
  historical artifact rather than deleted or fixed as part of this pass,
  since fixing dead code was not what was asked for here; the README now
  documents it as broken and superseded so nobody mistakes silence in its
  own output for a passing check.
- **`scripts/selftest.py`'s `test_context_budget()` is defined but not
  wired into `main()`.** The function exists in the file (computes
  worst-case prompt+output token totals per topology against `CTX_SIZE`) but
  is absent from the tuple of test functions `main()` actually calls, so it
  does not run as part of a normal `python3 scripts/selftest.py` invocation
  and contributes nothing to the "279 checks" total below. Not wired back in
  as part of this pass, for the same reason as above; documented as the
  file's actual current behavior, left as a decision for a future entry.

## 2026-08-24: Why `hotpotqa` reads near the ceiling: two hypotheses checked, both ruled out

Not a code change, an investigation, logged because it directly resolves
part of the open "near-ceiling" question in the entry below and should not
need repeating. Two candidate explanations for `hotpotqa`'s 65% baseline
sitting close to the 70% ceiling, checked against evidence rather than
argued from priors:

**Hypothesis: item selection skews easy.** Checked the `level` field
`prepare_datasets.py` already carries from the source HotpotQA rows but never
filters or stratifies on. All 80 frozen items are `level == "hard"`, HotpotQA's
own hardest official difficulty tier, unanimously. There is no easy-item
inflation to find; the draw could not have skewed easier than it did. Ruled out.

**Hypothesis: grading is too lenient, inflating the number.** Read `grade()`
and `span_correct()` in `datasets.py` and then, more usefully, read 30
individual (prediction, gold) pairs from `121313Z`'s `hotpotqa` baseline rows
against `ds.grade()`'s actual verdict. The grader is not lenient, several
"no" verdicts look like real misses on the grader's part, in the strict
direction:

- `pred='10'` vs `gold='ten'`, marked wrong. `_normalise()` lowercases,
  strips punctuation, and drops articles, but does not equate numeral and
  word forms. An unambiguous equivalence the grader currently can't see.
- `pred='animated film'` vs `gold='Animation'`, `pred='musicians'` vs
  `gold='musician'`, `pred='Stan Brakhage'` vs `gold='James Stanley
  Brakhage'`, synonym, plural, and nickname variants, all marked wrong.
- `pred='Shinjuku Eastside Square Building, Shinjuku, Tokyo'` vs
  `gold='Shinjuku'`, and `pred='...pharmaceutical companies'` (11 tokens) vs
  `gold='pharmaceutical companies'`, genuinely contain the gold phrase but
  exceed `contains_gold`'s length bound, which exists specifically to stop a
  verbose answer from winning by accident (see that function's own
  docstring). Working as designed here, at the model's expense for not
  following the prompt's brevity instruction, strict, not lenient.
- `pred='24 hours'` vs `gold='the full 24 hours'`, the gold itself is a
  padded paraphrase (4 tokens after normalising, so `gold_shape()`'s
  `SPAN_GOLD_MAX_TOKENS=4` boundary does not catch it as `long_span`) rather
  than a minimal span, so a terser correct answer cannot contain it verbatim.
  A boundary case `gold_shape()` currently misses, worth knowing about even
  though nothing here proposes changing the threshold.

None of this points to leniency. If anything, the measured 65% is more
likely a floor than a ceiling on the model's true accuracy, several
observed misses look like cases a human grader would credit.

**Conclusion.** Neither hypothesis explains the near-ceiling reading away.
The straightforward remaining explanation is the plain one: this model
handles hard-labelled `hotpotqa` questions well, close to or plausibly above
the band's 70% ceiling, and that is a real property of the model+task
combination rather than a measurement artefact.

**Decided, 2026-08-24: proceed without touching grading; document as a
limitation.** Full rationale in Standing Cautions below. Grading stays
exactly as frozen; nothing in `datasets.py` changed. The numeral/word-form
gap (`10`/`ten`) found above remains unfixed by choice, not oversight, fixing
it now would move `prompts_hash`-adjacent grading policy after seeing
results, which this file's own rules gate, and it would very likely push
`hotpotqa` further past the ceiling rather than resolve anything.

---

## 2026-08-24: Dry run at `--topology-items 20` (`121313Z`), and what the truncated calls actually were

Not a code change, evidence, plus the first real use of the `--debug-truncated`
capture added below. `--items 40 --topology-items 20 --debug-truncated
data/debug/truncated`. 748 calls, `config_hash 74b943de71fe5fd8` unchanged.
`selftest.py` confirmed 279/0-failed/2-warnings in Tanish's own Terminal
immediately before this run, see the entry below for why that specific
confirmation mattered.

**Resolved, with real power behind it this time.** `solver_critic` is not a
null topology on either dataset: `calls/it` 3.1 (`gsm_hard`) and 2.3
(`hotpotqa`), both clearly above the flat-accept floor of 2.0, accuracy 70%
and 65%. Unlike `130219Z` vs `131604Z`, this reading and `131604Z`'s used the
*same* mechanism at *different* topology-items counts (5 vs 20) rather than
different items at the same count, so this one is trustworthy where the
earlier disagreement wasn't. Revision-prompt-growth check passes both
datasets. Standing caution above updated accordingly.

**Confirmed, not noise.** `hotpotqa` debate answer-change rate is 10.0% again
but this time on 40 agent-rounds (4 changed), not 10. Same number twice at
4x the sample size is not the small-n coincidence `131604Z` left open; this is
a real reading. `hotpotqa`'s debate topology sits exactly on the >10% null-topology
floor. `gsm_hard` is fine at 30% (12/40). Needs a decision: accept `hotpotqa`
debate as a documented near-null result, or treat it as a prompt problem
(agents anchoring on their own first answer too strongly to move), the
second option changes `prompts_hash` and is the same category of call as the
`solver_critic` critic-prompt question, now with two instances of it.

**Item 3 closed out, read, not guessed at.** 7 of 748 calls hit `MAX_TOKENS`
(0.9%, down from `131604Z`'s 1.6% at the smaller `--topology-items`). All 7
raw prompt+completions are in `data/debug/truncated/` via the new
`--debug-truncated` flag. Two different failure modes, not one:

- `gsm_hard-00635` and `gsm_hard-01123` are the **same items** `gold_shape()`
  already flags `over_precise` in section 7 (golds `2040087.3384615383` and
  `3158933.272727273`). Confirmed by reading the completions: the model (and,
  on `00635`, the `solver_critic` critic role too) chases exact fractional
  precision through a non-terminating decimal and never lands, regardless of
  role or token budget. Already priced into the known 8.8-point unearnable
  figure, this is a symptom of a counted defect, not a new one. No action.
- `gsm_hard-01194` (gold `94`) and `gsm_hard-01252` (gold `-342927260`) are
  **not** flagged by `gold_shape()`, both golds are clean integers, but the
  model's intermediate arithmetic on both problems is messy enough (non-whole
  intermediate fractions, a self-correction detour on `01252` after finding
  `x = 66.67`) that it does not converge inside 512 tokens. This is real
  information loss on items with scorable answers, not a dataset defect. `1805`
  tokens of headroom exist under `CTX_SIZE` at the worst observed
  prompt+output (1267), so raising `MAX_TOKENS` (512 → 768 is one option) is
  safe on context-budget grounds. This moves `config_hash` and is the kind of
  change this file asks be argued for rather than made unilaterally, flagged
  for a decision, not applied.

**Unchanged.** Baseline accuracy: identical point estimates and intervals to
`131604Z` (`--items` did not change this run), still `UNRESOLVED` at every
cell, this still needs a dedicated larger `--items` pass, independent of
everything above. Format adherence 100% at every temperature, 0/748 retries.
Thinking-tag leaks 0/748. Gold shape unchanged (dataset-level).

---

## 2026-08-23: Post-131604Z: topology-items visibility and truncated-call capture

Direct response to the two "needs a decision" items the `131604Z` entry below
raised. `config_hash` and `prompts_hash` are both unchanged, neither touches
a `config.py` global, so `130219Z` and `131604Z` remain comparable to
whatever the next run produces; nothing here invalidates existing data.

**Item 2, the debate/topology-behaviour sample size, `dry_run.py`.** Not a
bug: `--topology-items` (default 5) already existed as a flag separate from
`--items`, and sections 3 and 4 of the report (debate change rate,
solver_critic revision growth, per-topology accuracy) are driven by it, not
by `--items`. `131604Z` bumping `--items` 10→40 added real power to section 2
(baseline accuracy) and did nothing for sections 3–4, the apparent
resolution of the `solver_critic` null-topology reading between the two runs
is `--items` changing which 5 items the stride lands on, not more evidence
accumulating on the same 5. The fix is report clarity, not code: sections 3
and 4 now print `--topology-items N per dataset, independent of --items` so
this can't be misread as scaling again. Pass `--topology-items` explicitly
alongside `--items` on any run meant to move sections 3–4, not just `--items`.

**Item 3, reading what a truncated call actually generated, `client.py` +
`dry_run.py`.** The CSV was never going to answer this: raw completion text
is not a measurement and was never a column (`records.py`'s schema is
deliberately narrow, see its own docstring on nothing derived being stored).
Added `LlamaClient(..., debug_truncated_dir=None)`; when set, `call()` writes
prompt+completion to a file there for any call whose `finish_reason ==
"limit"`, named by dataset/item/temperature/round so multiple runs don't
collide. `None` by default, zero behavioural difference to every existing
call path (`run_campaign.py`, `screen_datasets.py` neither pass it). The
write happens after `record` is built, which is after the trigger has already
gone low, so it cannot perturb a measured window even when enabled. Wired to
`dry_run.py` as `--debug-truncated <dir>`, also off by default.

**Verified discriminating.** Loosened the write guard from
`self.debug_truncated_dir and record.finish_reason == "limit"` to just
`self.debug_truncated_dir`, three checks in the new
`test_debug_truncated_capture` failed exactly as expected (an ordinary `eos`
call started writing a file, the file count for a single limited call went to
two, and the eos call's own prompt/completion leaked into a file that should
not exist). Reverted; all three pass clean.

**Verification.** `python3 scripts/selftest.py` run twice, once in the
sandbox this session edited from, once through the remote-devices bridge
against this repository's actual files on Tanish's machine (not the same as
running it in Tanish's own Terminal; the bridge runs inside its own Linux VM,
which is the same caveat the sysfs-permission bug story below turned on). Both
0 failed. Bridge run: 279 checks, 2 warnings. **Confirmed a third time in
Tanish's own Terminal** immediately before the `--items 40
--topology-items 20 --debug-truncated` run below: 279 checks, 0 failed, 2
warnings, identical to the bridge run. This is the one that counts per this
file's "gate for every change" rule, the other two were sandbox/bridge
environments, which is exactly the distinction the sysfs-permission bug story
above turned on, so it needed a real confirmation and now has one.

---

## 2026-08-23: Dry run at the current hash (`131604Z`)

Not a code change, evidence. Run at 4x the previous sample, `--items 40`,
directly to close two items the `130219Z` entry below left open.

368 calls, `config_hash 74b943de71fe5fd8`, `n_topology_items 5` unchanged.
`data/raw/dry_20260823T131604Z-74b943de71fe5fd8.calls.csv`.

**Resolved, tentatively.** `solver_critic` on `gsm_hard` no longer looks like
a null topology: `calls/it` is 2.8 against a flat-accept 2.0, meaning
revisions are now happening on a real fraction of items, and accuracy on that
cell is 80% against a 60% baseline. This is the same failure mode flagged at
n=10 (0/5 revisions) not reproducing at n=40, read as the earlier reading
being small-n noise, not as the prompt having been fixed, since nothing about
the critic prompt changed between runs. Revision-prompt growth check still
passes on both datasets (`gsm_hard` +387 vs an expected +380, `hotpotqa` +152
vs +146).

**New, needs a decision.** 6 of 368 calls (1.6%) hit `MAX_TOKENS` (512),
where `130219Z` had zero. Not spread across the run: all 6 are `gsm_hard`
`baseline` `solver` calls, and all 6 land on 4 items, 2 of which repeat at
two different temperatures (`gsm_hard-01123` at t=0.2 and t=0.7,
`gsm_hard-01252` at t=0.2 and t=0.7; `gsm_hard-00635` and `gsm_hard-01194`
once each at t=1.0). Every one of them still has `parse_ok=True` with an
answer extracted, but `truncated` (a separate column, driven by the server's
own signal) reads `False` on all of them, the two disagree, which itself
needs explaining before trusting either. Extracting a parseable number from a
generation the model was cut off mid-way through does not mean it is the
number the model was converging on; self-correction after the truncation
point cannot happen. Confined to one dataset, one role, four items out of 80,
so this reads as a small number of `gsm_hard` items provoking long solves
rather than a systemic `CTX_SIZE` problem, recommend reading the raw
completions for these four items before deciding whether to raise
`MAX_TOKENS` or leave it, since the fix depends on whether the model is
reasoning productively when it runs long or looping.

**New, needs a decision.** `hotpotqa` debate change rate is 10.0% (1 of 10
agent-rounds), against the >10% floor, flagged `NULL TOPOLOGY RISK` by the
report itself, where `130219Z` had it at 30%. The denominator here (10
agent-rounds) is the same at n=40 items as it was at n=10 items, unlike the
baseline-accuracy and solver_critic checks above which both scaled with
`--items`; unclear from the report alone whether this check subsamples a
fixed number of debate rounds by design or whether something upstream capped
it. Worth checking `dry_run.py` directly before treating a one-round swing
(10%, exactly on the floor, is one flipped round away from clearing it) as a
real finding rather than the same small-n noise that inflated `130219Z`'s
number.

**Confirms, not yet resolves.** Baseline accuracy point estimates now sit
inside the 45-70% band on both datasets, `gsm_hard` 55/57.5/60% across
t=0.2/0.7/1.0, `hotpotqa` flat at 65% across all three, where `130219Z` had
`hotpotqa` reading below the floor at every temperature. The Wilson interval
is still `UNRESOLVED` at every cell (29-point interval against a 25-point
band); the report states 96 items narrows to ±10, 381 to ±5. Reads as the
`130219Z` below-band reading having been small-n noise rather than a real
below-band signal, but "reads as" is not the same as resolved, this still
needs either a dedicated larger run or acceptance that the full campaign's
own N will resolve it before results are used for anything.

**Unchanged.** 100% parseable at every temperature, 0 retries in 368 calls, 
same pattern as `130219Z`, now at 2x the sample, still nothing for the
temperature→retry_count→energy mechanism to show. Thinking-tag leaks 0/368.
Gold shape unchanged (dataset-level, not sample-size-dependent): 7/80
`gsm_hard` over-precise, 8/80 `hotpotqa` long-span.

---

## 2026-08-23: Dry run at the current hash (`130219Z`)

Not a code change, evidence. Logged here because it directly bears on two
items already open below, and because the file's own purpose is knowing
whether something you're looking at is explained by an entry here.

184 calls, `config_hash 74b943de71fe5fd8`, `scripts/dry_run.py` at its default
`--items 10`. Everything below is n=10 per cell and is a smoke test, not a
decision, read accordingly.

**Clean.** Token headroom 2362 of 3072 at the worst observed 710. Zero
thinking-tag leaks, zero truncations, zero `MAX_TOKENS` hits across all 184
calls. Debate change rate 50% (`gsm_hard`) / 30% (`hotpotqa`), both well clear
of the 10% null-topology floor, the own-prior fix is holding.

**Worth watching, not acting on.** 100% parseable at every temperature
including `t=1.0`, 0 retries in 184 calls. The rule in this file is that
parse-failure rate rising with temperature is the mechanism RQ2
(temperature → retry_count → energy) depends on; at n=20 per cell there is
nothing yet for that mechanism to show. Flagged so a larger run is read
against this baseline rather than treated as a new finding on its own.

**Open, needs a prompt decision.** `solver_critic` on `gsm_hard`: 0 of 5
revisions, every first critique `ACCEPT`. Same failure mode already named
below, reconfirmed live at the current hash. `hotpotqa`'s `solver_critic` did
revise this run, prompt grew 152 tokens against an expected 146, so the
critique-reaches-solver fix is confirmed working on at least one dataset;
`gsm_hard` is the one still collapsing to baseline plus a wasted call. Needs a
critic prompt that re-derives rather than reviews, which changes
`prompts_hash` and is a scientific call, not a code fix.

**Open, needs more data, not a decision yet.** Baseline accuracy at n=10 is
`UNRESOLVED` at every cell by design (52-point Wilson interval against a
25-point band). Point estimates: `gsm_hard` 50/60/70% across
t=0.2/0.7/1.0, centred in band. `hotpotqa` 30/30/40%, below the 45% floor at
every temperature tested, though the interval cannot rule out in-band.
Section 7's `gold_shape()` found 8/80 `hotpotqa` golds are whole-sentence
spans the grader structurally cannot match (~10 unearnable points) and 7/80
`gsm_hard` golds are over-precise (~9 points), both model-blind, neither
applied as a filter. Next step is rerunning with more items (`--items 40`
roughly halves the interval) before deciding whether `hotpotqa` needs a
prompt or dataset change.

---

## 2026-08-23: Phase 1.4: the three hardware interfaces

`Trigger`, `Device` and `EnergyMeter` existed as no-op base classes so the
orchestrator could be proven on a laptop before the hardware existed. `Device`
became real in Phase 1.3; this closes the remaining two, plus the frequency
columns Phase 1.3 left at `0`.

**Hash change: `c704ed501758a150` → `74b943de71fe5fd8`.** Six new constants, 
`TRIGGER_CHIP`, `TRIGGER_LINE`, `TRIGGER_CONSUMER`, `METER_POLL_S`,
`METER_RATE_FLOOR_HZ`, `HW_FAULT_ALERT_EVERY`, plus `TRIGGER_CHIP` and
`TRIGGER_LINE` joining `REQUIRED_BEFORE_RUN`. No behavioural difference to any
existing call.

### Added `src/masenergy/gpio.py` and `src/masenergy/ina3221.py`

Two pure drivers, no project imports, so they can be exercised against
synthetic devices exactly like `discover_zones` already was. `jetson.py`
composes them into `JetsonTrigger` and `JetsonEnergyMeter`; `client.py` grew
about ten lines to read the new dict keys and stayed the file a reviewer reads
to be convinced the trigger brackets exactly one call.

**`gpio.py`, GPIO v2 character-device ABI, `ctypes` + `fcntl`, no
`Jetson.GPIO`, no `libgpiod` subprocess.** The line is requested once at
construction and the fd held for the run; `high()`/`low()` are one ioctl each
on a preallocated struct, so nothing is opened or allocated between the edge
and the HTTP send.

**`ina3221.py`, onboard rails from hwmon, found by label, never by channel
index.** Channel order is a device-tree property and renumbers across JetPack
releases; a reader wired to `curr1_input` silently reports a different rail
after an upgrade. The sampler runs continuously from construction to `close()`
rather than starting and stopping around each call, a sampler that only ran
during calls would not be running during `measure_idle`, biasing every energy
figure upward by the cost of the instrument with nothing to subtract it back
out.

### `energy_j_external` is never produced on the Jetson

The external rig's INA226 is on the ESP32's I2C bus, and the ESP32 is
deliberately hosted by the logging laptop, Build Brief constraint 2, so its
own draw stays outside the shunt. The Jetson has no path to that chip at all.
`energy_j_external` and `idle_w_external` are `NaN` on every row this campaign
writes; the join happens offline, by `trigger_pulse_n`, which is why that
column exists.

### Fault visibility: `NaN` not `0.0`, plus a closed-vocabulary `hw_status`

A hardware read that fails records and continues rather than raising and
killing the run, but has to stay distinguishable from a real reading, or a
starved sampler that collected four samples looks exactly like a quiet call.
Four layers:

- **Failed physical scalars are `NaN`, never `0.0`**, extending the rule
  `JetsonDevice` already used for absent thermal zones.
- **`hw_status`** is a closed vocabulary (`records.HW_FAULTS`), an unknown
  token raises rather than being written, so a typo in a fault name can never
  appear in any count of itself.
- **Evidence columns travel with the reading**: `meter_samples_n`,
  `meter_rate_hz`, `meter_window_s`, `trigger_pulse_n`, `trigger_edge_us`. This
  is the layer that catches the failure that raises nothing at all, a window
  that collected two samples instead of two thousand.
- **Consecutive-fault escalation.** `client.py` counts consecutive faulted
  calls and writes to stderr every `HW_FAULT_ALERT_EVERY`, so an instrument
  that came unplugged overnight is visible in the terminal, not only in a
  column nobody reads until the campaign ends.

### `is_stub()` tightened from `all()` to `any()`: `run_campaign.py`

The Phase 1.2 check asked whether *every* measured method was still the base
implementation. It missed the harder accident: a `Trigger` that overrides
`high()`/`low()` but inherits `status()` drives a real GPIO line and fills
every energy column, while reporting pulse `0` on every row, the join to the
external rig is destroyed and every other column looks populated. `any()`
refuses on a single inherited method. `close()` is deliberately excluded from
the measured set: it is lifecycle, not measurement, and a real implementation
with nothing to release is entitled to inherit the no-op.

**Verified discriminating.** A `HalfTrigger` that overrides `high()`/`low()`
only is asserted a stub; reverting the `any()` back to `all()` fails exactly
that check.

### GPIO ABI checked against the kernel headers, not memory

`linux/gpio.h` was read directly (`gcc` + `offsetof`) to get the real struct
sizes and ioctl request numbers, rather than transcribing them. Both are
asserted in `gpio.abi_mismatches()`.

**One check wasn't enough on its own.** A first pass asserted only
`sizeof()`. Dropping `event_buffer_size` from `gpio_v2_line_request` left
`sizeof` unchanged at 592 bytes, the four bytes are reclaimed by trailing
alignment padding, while every field after the hole silently shifted. Field
*offsets* are asserted now, not just struct sizes, because that is exactly the
mutation size alone missed.

### Bug found after initial delivery: `Path.exists()` on a forbidden sysfs path

`read_frequencies()`'s EMC fallback probed
`/sys/kernel/debug/bpmp/debug/clk/emc/rate` with `.exists()` before reading
it. That path is root-only; `.exists()` calls `stat()`, and `stat()` on a
directory this user cannot traverse raises `PermissionError` rather than
returning `False`. Run unprivileged, which is how the campaign runs, this
would have taken down `read_state()`, and therefore every call, the first
time it ran. **Found by running the suite on a different machine, not by
reasoning about the code.** Every sysfs probe in `jetson.py` and `ina3221.py`
now attempts the read directly and catches `OSError`, rather than testing for
existence first.

**Verified discriminating.** `test_sysfs_permissions` builds a `chmod 000`
directory and asserts `read_frequencies()` survives it with `freq_unreadable`
in the fault list rather than raising. It downgrades to a warning under root,
where the case can't be simulated.

### `check_device.py` extended for bring-up

`--gpio` lists every chip and the lines free to claim, lines already held by
a driver are omitted, so claiming one fails at startup rather than at the
first call. `--rails` lists the INA3221 channels this board actually exposes
and any label `RAIL_ALIASES` doesn't recognise. `--freq` shows which clock
paths resolved. `TRIGGER_CHIP`, `TRIGGER_LINE` and any rail-alias fix all come
from this output, pasted in, none of the three can be chosen from a laptop.

**Verified discriminating, nine mutations, all caught**, each checked against
a specific failing assertion: a partial integral instead of `NaN`; a failed
rail recorded as `0.0`; rails resolved by channel order instead of label; the
dropped-field GPIO struct above; `hw_status` accepting an unknown token; the
pulse ordinal not reaching the record; `energy_j_external` fabricated as
`0.0`; the half-implemented trigger; a rail exception escaping the sampler
thread silently.

### Known gaps: unverifiable without the rig or the Jetson

- `TRIGGER_CHIP` / `TRIGGER_LINE` are `None`. The line numbering is a
  device-tree property of the carrier board and cannot be guessed; a wrong
  guess either fails to claim or drives the wrong pin. Filled from
  `check_device.py --gpio` on the device.
- `RAIL_ALIASES` covers the label spellings seen across Orin carrier revisions
  and JetPack releases, but has never been checked against this board's actual
  hwmon labels. `check_device.py --rails` reports any label it doesn't match.
- The ioctl calls themselves have never reached a real `/dev/gpiochip*`. The
  struct layout is checked against the kernel headers (see above); whether the
  pin actually toggles is unverified.

---

## 2026-08-23: Post-1.3: de-risking the two untested paths

Not a numbered task. Two things had no coverage and both sit directly under a
ten-day run, so they were closed before moving on. No hash change.

### The campaign had never run past one block

`test_runner_end_to_end` exercised a single block. Nothing had ever run all 24,
and nothing had ever tested the property the design rests on, *"a crash on day
six must not cost six days"*. `--dry` cannot rehearse it while the frozen
parameters are unset.

**Added `test_full_campaign_and_resume`.** Runs every block against the stub with
a one-item set, interrupts partway using the real `_STOP` flag, then resumes with
a fresh `Runner` on the same directory and asserts: the interrupt released the
writer, the two sessions sum to exactly the expected task count, no task was
duplicated, all 24 blocks produced both a task and a call table, and a finished
campaign resumes to zero work.

The frozen parameters are set for the duration by a `campaign_config` context
manager and restored afterwards. This is **not** a bypass of the Phase 1.1
guard, the values are set, the run is real, `validate()` passes on its merits.

**Verified discriminating.** Breaking `completed_tasks()` to always return an
empty set produces 5 failures with exact diagnostics: `19 + 72 != 72`,
`91 task rows`, `19 duplicates`.

### `JetsonDevice` had no coverage of its sysfs half

Its policy was tested against injected clocks and its off-target refusal was
tested, but no line that reads a file had ever run.

**Added `test_jetson_sysfs`.** Builds a synthetic thermal tree shaped like the
kernel's and asserts discovery by reported name, that `tj-therm` is preferred
over `SOC0-therm` for the gate, millidegree conversion, that absent sensors give
`NaN` rather than a plausible zero, fallback when the junction sensor is missing,
and that a tree with zones but no SoC zone still refuses.

This cannot prove an Orin reports these zone names. It does prove the discovery,
ordering and unit conversion, which is everything except the names.

### Added `scripts/check_device.py`

First contact with the Jetson, and the answer to a question that currently
blocks the campaign.

`THERMAL_TARGET_C` has to be chosen and cannot be chosen blind: **a target below
the idle floor is never reached by cooling, and one above the loaded ceiling is
never reached by warming, either gates every call into a 300-second timeout.**
The script samples the device and reports the range it actually occupies, and
warns if observed drift already exceeds `THERMAL_TOLERANCE_C`, because a band
narrower than the sensor's own noise cannot be held.

```
python3 scripts/check_device.py                 what the device reports
python3 scripts/check_device.py --sample 120    watch it drift under load
python3 scripts/check_device.py --gate 50       exercise the real gate
```

Exercised against a synthetic tree in both directions: a reachable target
returns `gate_wait_s 0.00, gate_timed_out False`, and an unreachable one returns
`300.00 / True` after a genuine 300 seconds of wall clock, the timeout verified
against a real clock, not only an injected one.

**One bug found and fixed in the script itself** during that run: a two-line
`print` applied `% low` to the wrong line, raising `TypeError` mid-report.

---

## 2026-08-23: Phase 1.3: thermal gating

`config.BLOCK_SETTLE_S` and `config.THERMAL_TIMEOUT_S` were read by nothing and
`Device.wait_for_gate()` was a no-op, so the confound the randomised block order
exists to control was unmitigated.

**Hash change: `295678eeb8606cb8` → `c704ed501758a150`.** Two new constants,
`THERMAL_POLL_S = 2.0` and `SETTLE_POLL_S = 1.0`. No call behaves differently,
but the hash is the hash.

### Added `src/masenergy/jetson.py`

The first real hardware implementation in the repository. `JetsonDevice`
overrides `wait_for_gate()` and `read_state()`, so `run_campaign.py`'s stub
check now reports only `Trigger` and `EnergyMeter` as outstanding.

**It refuses to construct off-target.** `__init__` discovers thermal zones and
raises `ThermalUnavailable` if the SoC zone is absent. This is the whole
argument for the module existing separately from `client.py`: a Device that
quietly reported `0.0` degrees on a machine with no thermal zones would pass
every gate, satisfy every check, and write a campaign in which temperature was
never controlled with nothing in the data to say so.

**Zones are found by name, not by index.** `thermal_zone0` is not stable across
JetPack releases or across boots. A gate wired to an index silently starts
reading a different sensor after an upgrade.

**Millidegrees.** The kernel reports millidegrees; reading the file as degrees
is a factor-of-1000 error that would hold the gate open until it timed out on
every call. Asserted in the self test.

### `wait_until_in_band()`: the gate policy

Pure function, clock and temperature source injected, so a five-minute timeout
is exercised without waiting five minutes and the cold branch can be tested at
all. A device cannot be made cold on demand.

**Why cold matters as much as hot.** Below the band the silicon leaks less, so
static power is lower, and the governor sees thermal headroom and holds a boost
state it cannot sustain once the die warms. A cold call is both faster and drawn
at a different point on the voltage-frequency curve than the same call ten
minutes later. Above the band the governor throttles instead. Either way the
joules attributed to a token depend on when in the block the call landed.

That would be tolerable as noise. It is not noise: a device is coldest at the
*start* of a block, so the bias is aligned with block boundaries, and block
boundaries are where condition and temperature change. Randomising block order
stops thermal drift correlating with condition across ten days; it does nothing
about a within-block warm-up ramp that repeats identically in every block.

**Timeout rather than hang.** A run that hangs because a fan failed on day six
has lost six days. A run that records `gate_timed_out` on the affected rows can
be filtered at analysis time and keeps everything else.

**Verified discriminating.** Replacing `abs(temperature - target) <= tolerance`
with the one-directional `temperature <= target + tolerance` fails exactly one
check, *"a cold device is held until it warms into band"*, with the diagnostic
`a gate that only watches for overheating returns 0.0 here`.

### `Runner.settle()`: between blocks, interruptible

The per-call gate keeps calls comparable *inside* a block. It cannot make two
blocks comparable, because the device arrives at a new block carrying whatever
the previous condition left in it, and conditions differ in how hard they drive
the GPU. Settling drains that history before the next block's first call rather
than letting it decay across the block's early items.

Polled at `SETTLE_POLL_S` rather than slept in one piece. A five-minute
uninterruptible sleep between 24 blocks is two hours in which the operator's
only option is to kill the process and lose the task in flight.

**Verified discriminating.** Replacing the polled loop with a single
`time.sleep(total)` fails *"an interrupt cuts a settle short rather than waiting
it out"*, and the suite visibly takes 30 seconds longer doing it.

### `gate_wait_s` and `gate_timed_out` confirmed reaching records

Already plumbed in `client.call()`; now asserted, using a device that returns
known non-zero values so the assertion cannot pass on defaults. Also confirmed
the gate runs **before** `trigger.high()`, so waiting is outside the measured
window and does not appear as energy attributed to a call.

### Known gap: frequencies are still unpopulated

`read_state()` reports temperatures, nvpmodel and fan PWM, but `freq_gpu`,
`freq_cpu` and `freq_emc` remain `0`. The Tegra devfreq paths move between
JetPack releases and are not verifiable from here; shipping guessed paths whose
failure mode is a plausible zero is worse than shipping nothing. **Do not read
0 in those columns as a measurement.** Absent sensors elsewhere report `NaN`
rather than `0.0`, because a missing sensor and a sensor reading zero degrees
have to be distinguishable and `0.0` is a temperature.

---

## 2026-08-23: Phase 1.2: campaign entry point

### Added `scripts/run_campaign.py`

`Runner` was a class nothing instantiated. There was no supported way to start
the campaign, and therefore no place where the hardware objects were assembled
or checked.

**Blast radius.** New file, plus 15 checks in `selftest.py` and a section in
`README.md`. Nothing existing imports it. **No hash change**, it reads config,
it does not define it.

**Five gates, in order, each verified to fire:**

| Gate | Refusal |
|---|---|
| `config.validate()` | names all unset `REQUIRED_BEFORE_RUN` values |
| Hardware | `Trigger, Device, EnergyMeter are still the stub implementations` |
| `resolve_run()` | refuses `--resume` whose trailing config hash is not current |
| `LlamaClient.health()` | refuses if no server answers |
| `Runner.run()` | validates again (Phase 1.1) |

**Why hardware is detected by method identity, not class identity.**
`NullTrigger`, `NullDevice` and `NullEnergyMeter` are empty `pass` subclasses of
`Trigger`, `Device` and `EnergyMeter`, which are themselves no-ops. The two
families are indistinguishable by class, so the acceptance criterion "never the
`Null*` ones by accident" cannot be met by choosing a class. A check on
`type(obj)` would pass the moment anyone wrote `class JetsonTrigger(Trigger):
pass`, and the campaign would record zero joules against 20,160 calls while
looking exactly like a real run. `is_stub()` instead asks whether every measured
method is still the base implementation, so it starts passing only when a
subclass actually overrides the code that reads the hardware.

**Verified discriminating.** `is_stub()` was replaced with the plausible wrong
version, `type(instance) in (Trigger, Device, EnergyMeter, NullTrigger,
NullDevice, NullEnergyMeter)`, and the suite failed exactly the check written to
catch it: *"a subclass that overrides nothing is still a stub"*. The two checks
either side of it still passed, which is the point: only the class-identity hole
is exposed. Restoring the real implementation returns 180/0.

**Known consequence, `--dry` is gated too, and currently refuses.**
`--dry` does not bypass `config.validate()`, so a structural rehearsal is not
possible until `THERMAL_TARGET_C`, `NVPMODEL_MODE` and the three `PRICE_*` values
are set. This is deliberate: a rehearsal under a configuration the campaign
cannot use is not rehearsing the campaign, and bypassing the check here would
defeat the guard installed in Phase 1.1. It also cannot be bypassed without
changing `Runner.run()`. **Two escape routes if a rehearsal is wanted before
Phase 3:** set placeholder values in `config.py` and note them, or defer the
rehearsal. Do not add a bypass.

**Resumability, added beyond the brief.** `run_id` is timestamped, so
re-invoking after an interrupt would write to a *new* directory, find no
completed tasks, and silently redo work already on disk. `Runner` was resumable
by construction but resumption was unreachable from any entry point. `--resume
<run_id>` makes it explicit, and refuses a `run_id` whose trailing config hash is
not the current one, because rows written under two hashes must not land in one
directory.

**`--dry` output isolation.** Rehearsals write to `data/raw/rehearsal-<run_id>`
so zero-joule rows can never be pooled with a measurement.

**Cleanups during review.** An unused `datasets as ds` import and a
function-local `new_run_id` import were removed before the file was written.

## 2026-08-23: Phase 1.1: the validate guard

### `Runner.run()` now calls `config.validate()` first

`validate()` existed from the beginning and was reachable only from `config.py`'s
own `__main__` demo. Nothing on the execution path called it. A campaign could
begin with `THERMAL_TARGET_C` at `None` and produce ten days of ungated data
indistinguishable from the real thing.

**Files.** `src/masenergy/runner.py`, `scripts/selftest.py`.
**Blast radius.** `Runner.run()` now raises on this repo as it stands, because
five parameters are still unset. `Runner.run_block()` is unaffected, so the dry
run and the self-test's end-to-end block still work.
**No hash change**, `validate()` reads config, it does not define it.

**Verified discriminating.** With the `config.validate()` line deleted, all four
new `GUARD` checks fail and the run issues **869 stub model calls and writes real
block tables** before returning. Restoring the line returns 165/0. The test drives
`Runner.run()`, not `validate()` directly, because a unit check on `validate()`
passed throughout the entire period the defect was live.

---

## 2026-08-23: Grading, sampling and topology repair

Prompted by both datasets reporting below the 45–70% band in dry runs. The
diagnosis was that the measurement, not the datasets, was out of band. Run
`python3 scripts/diagnose.py` for the full evidence.

> **Grading was changed after data existed.** This is normally forbidden. The
> argument for it is that the previous numeric rule was not a stricter policy but
> an incoherent one, it meant two different things at two magnitudes, and that
> the change was made against recorded answers with the decomposition published
> in `diagnose.py` rather than tuned until the datasets landed in band. Anyone
> reviewing this should check that decomposition themselves.

### Numeric tolerance is relative, not absolute: `src/masenergy/datasets.py`

Was `abs(pred - gold) < 1e-6` at every magnitude. On an 11-digit integer that
demands exactness; on a gold of `2.0107e-06` it accepts a 50% error. Seven of the
80 `gsm_hard` golds carry up to 17 decimal places.

**Effect.** Re-scoring the *same recorded answers*: `gsm_hard` +13 to +17 points,
40% → ~57%. `hotpotqa` unaffected.
**Constant.** `NUMERIC_REL_TOL = 1e-4`, named and tunable. `diagnose.py` prices
the 1e-3 alternative at +2 points.

### `_NUMBER` could not match scientific notation: `datasets.py`

`-?\$?\d[\d,]*\.?\d*` extracted `06` from a gold of `2.0107e-06`. Now matches
scientific notation and leading-dot decimals.

### Added `gold_shape()`: `datasets.py`

Classifies a *reference* as `ok`, `long_span`, `over_precise` or `unparseable`.
Model-blind by construction, it never sees a prediction, so filtering on it does
not violate the rule against selecting items on observed accuracy.
**Findings.** 7/80 `gsm_hard` golds over-precise; 8/80 `hotpotqa` golds are whole
sentences. ~9–10 points per dataset the model cannot earn.
**Not applied as a filter.** Recommended, not done.

### `span_correct()` deliberately left alone

One-way containment costs ~9 points on `hotpotqa` (gold `It was held in France
from 10 June to 12 July 1998.` vs prediction `France`). It is a stated scientific
choice, not a defect, so it is priced in `diagnose.py` and not silently widened.

### Debate agents never saw their own prior answer: `topologies/debate.py`

The round-two prompt said "reconsider your own answer" and never included it. The
only answer in context was the peer's, and agents adopted it: 25% of paired rounds
ended with the two agents holding each other's answers exactly. This registers as
a *healthy* change rate in `dry_run` check 3.
**Fix.** `config.DEBATE_SHOWS_OWN_PRIOR` (default `True`).
**Partial.** 3 of 10 post-fix paired rounds still swap, on near-ties differing in
the last digit. Different phenomenon from wholesale adoption, but n is too small
to call settled. **Re-measure before trusting any debate energy number.**

### Workers never saw the problem: `topologies/planner_worker.py`

Planner wrote `How much did Mishka spend on the shorts?` and kept the prices. 48%
of worker calls produced nothing parseable and burned every retry, so affected
items cost nearly double the calls of their neighbours, an item-dependent energy
confound in the primary measurement.
**Fix.** `config.PLANNER_WORKER_SHOWS_TASK` (default `True`).
**Effect.** Worker failure 48% → 0%; calls per item 5.00 → 4.00, the structural
minimum. Verified live.

### Samplers pinned: `client.py`, `config.py`

`min_p` (llama.cpp default 0.05), `typical_p`, `repeat_penalty`,
`presence_penalty`, `frequency_penalty` and `mirostat` were left to server
defaults, so they ran uncontrolled and `config_hash()` never saw them. `min_p`
0.05 clips exactly the distribution tail the temperature sweep exists to widen.
**This is the change that moved `config_hash` to `295678eeb8606cb8`.**

### Record schema: `records.py`, `client.py`

- `cached_n` → `slot_cache_n`. It was llama.cpp's post-generation slot occupancy
  (`≈ prompt_n + predicted_n`), not a cache-hit count. The old name invited
  reading it as evidence of prompt caching.
- Added `prompt_n_total` from `tokens_evaluated`, and `call()` now **raises** if
  `prompt_n != prompt_n_total` while `CACHE_PROMPT` is false, a live prompt cache
  now stops the run instead of silently attributing energy to unprocessed tokens.
- Added `thinking_leak`, set from the raw response.
- Removed `correct` and `f1` from `CallRecord`. Nothing ever wrote them, so every
  call row read `False`/`0.0`. Grading is a property of a task and lives in the
  task table.
- `RecordWriter` now refuses to append to a file whose header does not match.

### The thinking-suppression check could never fire: `dry_run.py`

It searched `answer_extracted` for `<think>`, but `_unwrap()` strips every
angle-bracket span before storing. It reported zero regardless of what the model
emitted. Now reads the `thinking_leak` record field.

### The truncation check read the wrong column: `dry_run.py`, `screen_datasets.py`

`truncated` means the *prompt* overran the context, which `--no-context-shift`
turns into an error, so it is `False` on every row ever written. Length-capped
generations report `stop_type == "limit"`. **28 recorded generations hit
MAX_TOKENS while both reports printed `truncated responses: 0`**, output length
was being censored in the distribution the study exists to measure.

### Band verdicts now carry an interval: new `src/masenergy/band.py`

The screen ran 15 items against a 25-point band. At n=15 the 95% Wilson interval
is ~45 points wide, so **no result the screen could have produced would have
decided anything**. Both survivors were admitted on a number that could not
distinguish 30% from 75%. Demonstrated, not argued: the same dataset, weights and
grader land anywhere from 6.7% to 66.7% across recorded runs.
**Fix.** `verdict()` returns `UNRESOLVED` when the interval straddles a band edge.
`N_ITEMS = 80` gives ±10 points; ±5 needs 381. Self-test warns about this.

### Sampling made explicitly nested: `datasets.nested_sample()`

The screen and the preparer each called `random.sample` independently. They
happen to nest for the current sizes only because CPython picks one algorithm at
both; it switches above a threshold and the nesting stops holding with nothing to
show it. Now one shared shuffled-prefix draw.
**Consequence.** Re-running `prepare_datasets.py` would now draw a *different* 80
items. The committed files still load and verify. `write()` refuses to overwrite
without `--force`. **Do not regenerate one dataset mid-campaign.**

### Other fixes

| What | Where |
|---|---|
| `client.writer` left bound to a closed writer between blocks | `runner.py` |
| ETA counted blocks passed, not tasks left; wrong on resume | `runner.py` |
| Planner's subtask list reached the CSV as a Python `repr` | `client.py` |
| A 200 response carrying an `error` body was treated as an empty completion, spending three retries | `client.py` |
| `serve_dev.sh` hard-coded ctx 8192 against `CTX_SIZE` 3072, and `LLAMA_FLAGS` launched nothing | `serve_dev.sh` |
| `dry_run` pooled the topology sweep's baseline into the t=0.7 temperature cell | `dry_run.py` |

---

## Standing cautions

- **Whether the frozen model configuration fits and stays resident in the
  Jetson's 8 GB is estimated, not confirmed.** Mac-side investigation
  (2026-08-26) puts the real memory commitment at roughly 4.2 GB (GGUF
  header math and live `vmmap` agree), not the 13.56 GB an initial
  Activity Monitor reading suggested. That number came from macOS's Metal
  backend; Jetson runs CUDA, and the backend-overhead slice on top of the
  3.8 GB weights-plus-KV-cache floor is not guaranteed to transfer.
  Separately, the same investigation directly observed this Mac's
  `llama-server` process being swapped to disk under ordinary
  multi-app memory pressure, not merely slowed, which would corrupt
  trigger-bracketed timing if it happened mid-call on the Jetson.
  `check_device.py` currently has no memory check at all (only thermal
  zone names, GPIO chip/line discovery, and INA3221 hwmon labels); adding
  one, watching `tegrastats`/`/proc/meminfo` during a real call on the
  physical device, is the only way to close this for real rather than by
  analogy to a 24 GB machine with a browser and an IDE competing for RAM.
- **`items_gsm8k.json` is still in `data/items/`** and is not in
  `config.DATASETS`. A stale item file in the directory the runner globs.
- **`Trigger` and `EnergyMeter` are real as of Phase 1.4, but unverified on
  target.** `JetsonTrigger`'s ioctl layout is checked against the kernel
  headers, not against a real `/dev/gpiochip*`. `JetsonEnergyMeter`'s rail
  labels (`RAIL_ALIASES`) have never been checked against this board's actual
  hwmon output. `TRIGGER_CHIP` and `TRIGGER_LINE` are `None` and block
  `validate()` until `check_device.py --gpio` on the device supplies them.
- **`JetsonDevice` has never run on a Jetson.** Policy, sysfs parsing and the
  off-target refusal are all tested, the last against a synthetic thermal tree.
  What remains unverified is whether an Orin reports the zone *names* in
  `SOC_ZONE_NAMES`. Run `scripts/check_device.py` on the device first; if the
  SoC zone is not found, add the name it does report.
- **`--dry` cannot run until the frozen parameters are chosen.** See Phase 1.2.
  The machinery it would rehearse is now covered by
  `test_full_campaign_and_resume`, so this is a convenience gap rather than a
  coverage gap.
- **`solver_critic` is not a null topology, as of `121313Z` (2026-08-24,
  `--topology-items 20`).** `calls/it` 3.1 (`gsm_hard`) and 2.3 (`hotpotqa`),
  both above the flat-accept floor of 2.0; revision-prompt-growth check passes
  both datasets. The `130219Z`/`131604Z` disagreement that used to live here
  was `--topology-items` staying at its default of 5 while `--items` changed
  which 5 items got sampled, not enough power to call it either way. This
  reading used 4x the topology-items and is trustworthy. Retest if the critic
  prompt ever changes.
- **`hotpotqa` debate answer-change rate was 10.0% at n=40 agent-rounds
  (`121313Z`, 2026-08-24), sitting exactly on the >10% null-topology floor.
  Decided 2026-08-24: treat as a prompt-engagement problem, not a documented
  near-null result, and fix it. Verified 2026-08-24.** `debate_agent.txt`'s
  reconsideration paragraph was rewritten to require an agent to check a
  peer's specific supporting evidence before keeping or changing its answer
  (`prompts_hash` moved `a80170cf67250404` -> `61e6c78bad256b91`). A dry run
  at the same `--topology-items 20` sample size (`--items 40
  --topology-items 20`) confirms it: `hotpotqa` change rate 10.0% -> 17.5%
  (7 of 40, clear of the floor rather than sitting on it), `debate`
  `hotpotqa` accuracy 60%, inside the 45-70% target band, `gsm_hard`'s
  already-healthy change rate held steady at 27.5% (was 30%). See the
  2026-08-24 verification entry for the full readout, including one
  unresolved minor side note (`MAX_TOKENS` hits ticked up from 0.9% to
  1.2% of all calls, not yet read from raw completions). This is confirmed
  at the sample size tested, not yet re-run at higher power; treat a
  `debate`/`hotpotqa` row under `61e6c78bad256b91` as measuring a topology
  that has now cleared its own bring-up gate, not as a statistically
  settled result the way the baseline accuracy band still isn't.
- **A `llama-server` predating this work was found running on port 8080**
  without `--no-cont-batching`; it blocked `serve_dev.sh` mid-session on
  2026-08-23 and was killed by hand. Not a code fix, so it can recur, if a
  future `serve_dev.sh` fails to bind, check for a stray process before
  assuming something in this repo changed.
- **`hotpotqa` baseline accuracy reads near the band's 70% ceiling (65%
  observed) and the Wilson interval does not resolve even at the full
  campaign's own N per cell (240, see the 2026-08-24 entry above for the
  arithmetic). Decision made 2026-08-24: proceed without touching grading;
  document as a limitation, not a defect.** Two explanations were checked and
  ruled out first, not assumed: item selection is not skewing easy (all 80
  frozen items are HotpotQA's own `level == "hard"` tier, unanimously), and
  grading is not lenient (reading actual prediction/gold pairs found the
  grader erring strict, numeral-vs-word forms like `10`/`ten` unequated,
  synonym/plural/nickname variants rejected, correct-but-verbose answers
  hitting `contains_gold`'s anti-gaming length bound). If anything the true
  rate is a floor, not a ceiling, on the measured 65%. For the eventual
  writeup: `hotpotqa` topology-vs-baseline *accuracy* comparisons should be
  read as less discriminating near this ceiling; energy and cost comparisons
  are unaffected by any of this and stay fully valid. Grading remains exactly
  as originally frozen, nothing in `datasets.py` changed as a result of this
  investigation, on purpose, per this file's own rule against tuning grading
  after results exist.

---

## Verification

```bash
python3 scripts/selftest.py     # gate for every change
python3 scripts/diagnose.py     # the above, plus every run on disk re-graded
```

Current: **279 checks, 0 failed, 2 warnings** (`--no-mmap` deprecation;
`N_ITEMS` resolves the band only to ±10 points). Both pre-existing and expected.
