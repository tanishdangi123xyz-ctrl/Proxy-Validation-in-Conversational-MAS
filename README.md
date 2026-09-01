# Conversational MAS Energy Measurement on Jetson Orin NX

## 1. Introduction

This repository is the codebase for a hardware energy-measurement study of
conversational multi-agent LLM systems ("MAS", multi-agent systems) running
on an NVIDIA Jetson Orin NX 8GB. It exists to answer a question the
literature on multi-agent LLM systems almost never measures directly: when a
debate, a solver-critic loop, or a planner-worker pipeline spends more
tokens and more wall-clock time than a single model call, does it actually
spend more *energy*, and by how much, measured on real hardware rather than
inferred from a token count and a per-token cost assumption?

Nearly every existing comparison of "efficient" versus "expensive"
multi-agent topologies uses token count or API dollar cost as a stand-in for
energy. Those proxies assume a roughly fixed joules-per-token rate, which
elides everything that actually varies call to call and topology to
topology: prompt-length-dependent prefill cost, decode-time GPU/CPU/memory
clock behaviour, thermal throttling under sustained load, idle/static power
that has nothing to do with any one call, and the retry and reprompt
overhead that a broken JSON parse or a temperature-induced format failure
adds invisibly. This project does not assume a joules-per-token rate. It
measures joules directly, on the device actually doing the inference, call
by call, with the accuracy and token-count bookkeeping needed to check
afterwards whether the token-based proxy the field usually relies on was
ever a good one.

The person running this project is both the sole software engineer and the
sole researcher on it: every module in `src/masenergy/` and every script in
`scripts/` was written to be read, audited, and defended, not just to run.
That is why the codebase is unusually heavy on docstrings that explain *why*
a design decision was made, not only what a function does, those docstrings
are treated as part of the scientific record, on the same footing as the
data, because a measurement pipeline whose reasoning cannot be re-derived
from its own source is not a pipeline anyone should trust the numbers from.

## 2. Description: what this project is

At its core, this is an experiment-automation pipeline plus a hardware
instrumentation layer. It is not a machine-learning training project and it
does not fine-tune anything. The model under test, Qwen3-1.7B, is used
exactly as released, at native BF16 precision (no quantization anywhere in
the study, quantization changes the arithmetic the GPU does per token,
which would confound the very thing being measured), served locally by
`llama.cpp` on the Jetson itself. Every experimental factor that could
influence how much energy a call costs, context size, sampler settings,
concurrency, prompt caching, the device's power mode and clock lock, the
thermal state the device is in when a call starts, is pinned to one value
for the entire campaign and stamped into a configuration hash that travels
with every recorded row, specifically so that no two rows in the final
dataset can differ for a reason nobody can name.

The repository has four layers, and the file layout mirrors them directly:

- **The orchestration core** (`src/masenergy/`), a dependency-free, Python
  3.10-compatible library that talks to the local `llama.cpp` HTTP server,
  runs one of four "topologies" (baseline single-call, debate,
  solver-critic, planner-worker) over a task, grades the resulting answer,
  and, critically, brackets every individual model call with a hardware
  trigger pulse and an energy integration window, so that energy is
  attributed to *one call*, never to a task, a block, or a session as a
  whole. This layer is written to run identically on a laptop (using no-op
  "Null" hardware stand-ins) and on the Jetson (using real GPIO, thermal
  sysfs, and INA3221 rail readers), which is what lets the whole pipeline be
  proven correct before the physical rig exists.
- **The dataset and grading logic** (also in `src/masenergy/`, principally
  `datasets.py`), loads two frozen, pre-sampled item sets (a numeric
  reasoning benchmark, `gsm_hard`, and a multi-hop question-answering
  benchmark, `hotpotqa`), extracts a final answer out of a model's raw text
  response, and grades it against the reference answer under rules that are
  identical regardless of which topology produced the answer.
- **Bring-up and verification tooling** (`scripts/`), scripts that answer,
  in order: does the pipeline work at all with no hardware and no server
  (`selftest.py`); is the model's behavior (format adherence, accuracy,
  topology behavior, token lengths) sane against a live server before any
  hardware is involved (`dry_run.py`); what does this specific physical
  Jetson actually report for temperature, GPIO lines, and power rails
  (`check_device.py`); and, historically, what do the recorded runs already
  on disk actually say happened, re-graded several different ways
  (`diagnose.py`). This layer is where the project's day-to-day engineering
  work has actually happened, the orchestration core has been comparatively
  stable, while the bring-up scripts have caught and driven the fix for
  every substantive bug found so far (see Section 5 and `CHANGES.md`).
- **The campaign driver** (`scripts/run_campaign.py` plus
  `src/masenergy/runner.py`), the only supported way to start or resume the
  actual ten-day measurement run. It is deliberately paranoid: it refuses to
  start unless every frozen parameter has actually been set, the item files
  on disk still hash to what the study recorded freezing, a live server is
  answering, and the hardware objects it has been handed are demonstrably
  not the no-op stand-ins the rest of the pipeline uses for cheap testing.

The experiment itself is a full factorial design: 2 datasets × 4 topologies
× 3 temperatures × 3 seeds × 80 items per dataset, for 24 "blocks," each run
in a randomised order so that any slow thermal or hardware drift over the
multi-day campaign cannot correlate with which topology or temperature
happens to run early versus late.

## 3. Repository file structure

The actual current layout, confirmed directly against the repository on
disk (not copied from an older draft, see the note on stale references
below):

```
.
├── .gitignore
├── .python-version-note
├── CHANGES.md
├── README.md
├── requirements.txt
├── analysis/
│   └── .gitkeep                      placeholder only, no code yet
├── data/
│   ├── debug/
│   │   └── truncated/                 populated only by --debug-truncated
│   ├── items/
│   │   ├── items_gsm8k.json           stale leftover, unused
│   │   ├── items_gsm_hard.json        frozen campaign set
│   │   └── items_hotpotqa.json        frozen campaign set
│   ├── processed/
│   │   └── .gitkeep                  placeholder only, no code yet
│   ├── raw/                          gitignored, campaign/dry-run output
│   └── screen/                       gitignored, Stage-1 screen working area
├── firmware/
│   └── esp32/
│       └── .gitkeep                  placeholder only, firmware not written yet
└── scripts/
    ├── check_device.py
    ├── diagnose.py
    ├── dry_run.py
    ├── find_model.py
    ├── prepare_datasets.py
    ├── run_campaign.py
    ├── screen_datasets.py
    ├── selftest.py
    ├── serve_dev.sh
    └── verify_fixes.py
```

```
src/masenergy/
├── __init__.py
├── band.py
├── chat.py
├── client.py
├── config.py
├── datasets.py
├── gpio.py
├── ina3221.py
├── jetson.py
├── prompts/
│   ├── baseline_solver.txt
│   ├── critic.txt
│   ├── debate_agent.txt
│   ├── debate_synthesiser.txt
│   ├── planner.txt
│   ├── planner_synthesiser.txt
│   ├── solver.txt
│   └── worker.txt
├── records.py
├── runner.py
└── topologies/
    ├── __init__.py
    ├── baseline.py
    ├── debate.py
    ├── planner_worker.py
    └── solver_critic.py
```

A stale-reference note, checked directly against the repository rather than
assumed: an earlier draft of this README referenced a `doc/` directory
(for a design-analysis document) and a `host/` directory (for laptop-side
ESP32 serial capture). Neither currently exists anywhere in this
repository, not even as a `.gitkeep` placeholder, unlike `analysis/`,
`firmware/esp32/`, and `data/processed/`, which do exist as placeholders.
Anyone looking for either should not expect to find it yet; this is a real
gap, not a broken link within this document.

## 4. Setup and running

Bring-up on a fresh checkout, in order:

```
scripts/serve_dev.sh models/Qwen3-1.7B-BF16.gguf
python3 scripts/run_campaign.py                    # start
python3 scripts/run_campaign.py --resume RUN_ID    # continue after an interrupt
python3 scripts/run_campaign.py --dry              # Null hardware, rehearsal only
```

Checking the pipeline, no server and no hardware required:

```
python3 scripts/selftest.py     # every check, exit status = failures
python3 scripts/diagnose.py     # the same, plus what the recorded runs say
python3 scripts/diagnose.py --brief --strict
```

The setup checklist below reflects this project's actual current status, as
established throughout Section 5 below, rather than an older draft's
checklist (which had left most steps unchecked despite them already being
done):

- [x] 1. Project skeleton
- [x] 2. Python environment (laptop: 3.14.3 in a venv; Jetson: system Python
  3.10 on JetPack 6 or 3.12 on JetPack 7, no venv, see
  `.python-version-note`)
- [x] 3. Config module (`config.py` exists and is enforced by
  `validate()`; still genuinely unset: `THERMAL_TARGET_C`, `TRIGGER_CHIP`,
  `TRIGGER_LINE`, `NVPMODEL_MODE`, `PRICE_IN_PER_M`, `PRICE_OUT_PER_M`,
  `PRICE_SOURCE`, see `config.REQUIRED_BEFORE_RUN`)
- [ ] 4. `llama.cpp` on the Jetson (works routinely via `serve_dev.sh` on
  development hardware; not yet confirmed running on the physical Jetson
  Orin NX itself)
- [ ] 5. Model download and throughput calibration (also covers whether
  the frozen configuration fits and stays resident in the Jetson's 8 GB,
  not just how fast it runs; Mac-side investigation on 2026-08-26 put the
  real memory commitment at roughly 4.2 GB, well short of an initial
  13.56 GB reading that turned out to be macOS memory-pressure noise, but
  that number came from macOS's Metal backend, not Jetson's CUDA, so it
  does not settle the question; see `CHANGES.md`, 2026-08-26, and the
  Standing Cautions there)
- [x] 6. `call()` and the record schema (`client.py`, `records.py`, 
  implemented and self-tested)
- [x] 7. Topologies (all four implemented, self-tested, and exercised
  against a live server via `dry_run.py`)
- [x] 8. Datasets and grading (`datasets.py`; both frozen item sets
  prepared and screened)
- [x] 9. Runner (`runner.py`, implemented and self-tested end to end,
  including resume)
- [ ] 10. Thermal gate (`jetson.py`'s policy is implemented and
  self-tested against synthetic data; never yet exercised against real
  sysfs on the physical device, see Section 5.7)
- [ ] 11. GPIO trigger (`gpio.py` is implemented and ABI-checked; never
  yet exercised against a real `/dev/gpiochip*`, see Section 5.7)
- [ ] 12. Host capture (not started; no `host/` directory exists yet)

## 5. What exactly the project is doing: in detail

### 5.1 The scientific question and why the design follows from it

The question is whether a multi-agent topology's *measured energy* tracks
the proxies people currently substitute for it, token count, wall-clock
time, and dollar cost computed from a price-per-token schedule. Answering
this requires, for every model call in the study, simultaneously knowing:
how many tokens went in and came out, how long the call took, how many
joules it drew on more than one independent instrument, what topology and
role that call played inside its task, and whether the device was in a
comparable thermal and clock state to every other call it will be compared
against. `records.py`'s `CallRecord` schema is the single place all of this
converges, one row per model call, never per task and never per topology, 
because the call is the unit every one of those comparisons is made at.

Two independent energy measurement paths exist by design, not by accident:
the Jetson's own onboard INA3221 power-monitoring chip (read via Linux's
`hwmon` sysfs interface, in `ina3221.py`/`jetson.py`), and an external
INA226-based power rig sitting on a shunt between the Jetson's supply and an
ESP32 microcontroller that samples it and is itself powered and logged by a
separate laptop. The external rig is the ground truth; the onboard rails are
what nearly every other paper in this space relies on instead of an external
rig, and part of this study's purpose is to check whether that reliance is
justified. Because the ESP32 sits outside the Jetson entirely, hosted by the
laptop, the instrument's own power draw is structurally excluded from the
measured domain, the Jetson has no code path that can even see that chip.
The join between an ESP32 sample and a Jetson call record happens after the
fact, by matching `trigger_pulse_n`, a GPIO line the Jetson raises for the
duration of exactly one HTTP call and which the ESP32 counts edges on, 
because the two devices keep independent clocks and cannot be synchronised
by timestamp.

### 5.2 The trigger-bracketed call, and why concurrency is disabled entirely

`client.py`'s `LlamaClient.call()` is the single choke point every model
call in the entire study passes through. Inside a module-level lock, the
sequence is: wait for the device's temperature to be inside a fixed target
band (in either direction, a device that is too *cold* is running at a
different point on its voltage/frequency curve than a warmed-up one, so
"too cold" is exactly as much of a confound as "too hot"), read device state
before the call, start the energy meter, raise the trigger line, issue the
one HTTP request to `llama.cpp`, lower the trigger line the instant the
response returns, stop the energy meter, and read device state after.
Nothing else happens inside that bracket. Concurrency is not merely
discouraged; it is made structurally impossible: a single Python
`threading.Lock` wraps the entire bracket, so even a topology whose
conceptual model is "run two agents in parallel" (debate's first round) is
executed strictly serially in wall-clock time, the parallelism in a debate
round is in what each agent is conditioned on, never in when its call
actually runs, because two overlapping trigger windows could never be
disentangled by a single GPIO edge.

### 5.3 The four topologies

All four topologies share one call primitive
(`client.call_with_retries()`), one grading rule per dataset, and one
record schema; they differ only in how many calls they make, what each call
is shown, and what role label each call carries.

- **`baseline`**: one call. The reference every other topology is measured
  against; not a "topology" in the same structural sense as the other
  three.
- **`debate`**: `DEBATE_AGENTS` (2) symmetric peer agents, each running for
  `DEBATE_ROUNDS` (2) rounds, followed by one synthesiser call that combines
  the final answers. After round one, each agent is shown its peers'
  previous answers and (governed by the `DEBATE_SHOWS_OWN_PRIOR` flag,
  currently `True`) its own previous answer, because every call is
  stateless, an agent asked to "reconsider your own answer" that is shown
  only a peer's answer has nothing of its own to reconsider, and will simply
  adopt whatever it was shown, which manufactures a large *apparent*
  answer-change rate with zero actual deliberation happening. This exact
  bug was found and fixed earlier in the project's history (see
  `CHANGES.md`) by measuring the answer-change rate and noticing debate
  looked suspiciously talkative for a topology that was, at the time, not
  being shown its own prior answer.
- **`solver_critic`**: a solver drafts an answer, a critic reviews it and
  returns `Verdict: ACCEPT` or `Verdict: REJECT` with reasoning, and if
  rejected the solver revises, shown the critic's *entire* response, not
  just the bare verdict, because passing only ACCEPT/REJECT would make this
  a blind retry loop wearing the costume of a feedback topology. This loops
  up to `SOLVER_CRITIC_MAX_ITERS` (3) times. Call count genuinely varies
  item to item, which is deliberate: this topology is the study's clearest
  probe of whether feedback-driven extra compute buys anything, in energy
  terms, over just retrying blindly.
- **`planner_worker`**: a planner decomposes the task into exactly
  `PLANNER_WORKER_SUBTASKS` (2) subtasks, each independent worker solves its
  subtask with no visibility into the other worker or the other worker's
  answer, and a synthesiser combines both results into a final answer. If
  the planner fails to produce a parseable numbered list after its retries,
  the workers are dispatched on the raw original task instead (so the call
  count and structure stay comparable across items even when planning
  itself failed), a fallback path this project added after discovering
  that a 1.7B model, told to "write self-contained subtasks," routinely
  omits information the sub-question needs (e.g. asking "how much did
  Mishka spend on the shorts?" without carrying forward the prices from the
  original problem), which used to burn every retry on an unanswerable
  worker call and silently double that item's cost relative to its
  neighbours. The current prompt (governed by
  `PLANNER_WORKER_SHOWS_TASK`, currently `True`) carries the full original
  problem alongside every worker's subtask as background context, while
  still keeping workers blind to each other.

### 5.4 Datasets, extraction and grading

Two frozen datasets are used: `gsm_hard` (numeric reasoning, from
`reasoning-machines/gsm-hard`, not plain GSM8K, because GSM8K's small
round-number operands make it too easy for this model and pushes accuracy
above the target band, whereas gsm-hard's large, awkward operands land it
inside the band) and `hotpotqa` (multi-hop question answering, `distractor`
config, restricted at preparation time to only the supporting/"gold"
paragraphs so the task is answerable from the given context). Both were
selected from a larger candidate pool (`gsm_hard`, `math500`, `drop`,
`hotpotqa`, `wiki2hop`, `musique`) by `scripts/screen_datasets.py`, which
measures baseline accuracy against the target band before any dataset is
committed to the frozen item files the campaign actually runs against
(`scripts/prepare_datasets.py`).

The target accuracy band, `band.py`, is 45–70%: the range within which a
multi-agent topology's extra deliberation actually has room to change the
outcome. Above 70% the model is already answering nearly everything
correctly and every topology collapses behaviourally into the baseline;
below 45% the critic and debate agents would be arguing over noise the model
cannot reliably produce at all. Because a small sample cannot resolve
whether an observed accuracy is really inside that band or merely looks
like it is by chance, `band.py` computes a 95% Wilson confidence interval
(not a normal approximation, which misbehaves near 0% and 100%) around the
observed rate and only declares `IN BAND`, `ABOVE BAND`, or `BELOW BAND`
when the whole interval falls unambiguously on one side of a boundary; when
the interval straddles a boundary the verdict is honestly reported as
`UNRESOLVED, n too small` rather than forcing a call the sample cannot
support. This project spent real effort establishing, with worked numbers,
that even the full campaign's own statistical power (240 datapoints per
cell) can leave a near-ceiling result such as `hotpotqa`'s ~65% observed
baseline accuracy formally `UNRESOLVED`, more data is not automatically the
fix, and that finding is logged in `CHANGES.md` rather than treated as a bug
to chase away.

Answer extraction (`datasets.extract_answer`) looks for the model's declared
final answer, always following an `"Answer:"` marker (every prompt in
`src/masenergy/prompts/` instructs the model to end with exactly that
format), unwraps LaTeX/markdown delimiters and boxed answers, and strips any
echoed template placeholder text, but it never guesses at content that
was not actually given, and a response with no usable final line is a
logged parse failure, which triggers a reprompt (up to `MAX_REPROMPTS`, 2,
extra attempts, each a fully separate, fully measured model call, retries
are data, not noise to be hidden, because the parse-failure rate rising with
temperature is one of the phenomena the study is explicitly checking for).
Grading (`datasets.grade`) is dataset-specific but topology-blind: `gsm_hard`
uses relative-tolerance numeric matching (`numbers_match`, 1e-4 relative
tolerance, chosen because gsm-hard golds span from small integers to
scientific-notation numbers with up to seventeen significant digits, so a
fixed absolute tolerance would be simultaneously too strict at large
magnitudes and absurdly loose at small ones); `hotpotqa` uses normalised
exact match or bounded-length containment of the gold phrase inside the
prediction (`span_correct`/`contains_gold`), where "bounded" specifically
exists to stop a long, rambling answer from scoring correct merely by
accidentally containing the gold string somewhere inside it. A separate,
model-blind classifier, `gold_shape`, flags reference answers the grader
can structurally never score correctly regardless of how good the model's
answer is (a gold phrase too long to be matched under the containment bound,
or a numeric gold with implausibly many decimal digits), this project's own
standing rule is that such items are reported as an instrument limitation on
the reference set, never quietly dropped or used to justify loosening the
grader after results are already in hand (see the Standing Cautions in
`CHANGES.md` for the fullest example of this discipline: a documented,
investigated, and deliberately *un-fixed* grading gap around numeral-vs-word
answer forms).

### 5.5 Hardware fault handling and the "NaN, never zero" rule

A failed hardware reading is always recorded as NaN, never as zero and never
silently omitted. Zero joules and zero degrees are both physically
meaningful readings, so writing zero for a measurement that simply did not
happen would make a broken sensor statistically indistinguishable from a
genuinely quiet device, and the worst version of that failure raises no
exception at all: a call whose energy meter thread died mid-window and
collected four samples instead of several hundred still produces a
plausible-looking energy figure, and only the sample-count column
(`meter_samples_n`) and the closed-vocabulary fault column (`hw_status`,
`records.HW_FAULTS`) say anything went wrong. Faults never halt a campaign, 
a ten-day run should not be lost to one day's fan controller going quiet, 
but they are loud: `client.py`'s `_hw_status` shouts to stderr once faults
have persisted for `HW_FAULT_ALERT_EVERY` (10) consecutive calls, so an
operator watching the terminal sees a genuinely stuck instrument rather than
discovering it only during post-hoc analysis of ten days of data.

### 5.6 The campaign structure, resumability, and thermal settling

`config.blocks()` is the cartesian product of 2 datasets × 4 conditions × 3
temperatures, 24 blocks, each executed as `N_ITEMS` (80) items × 3 seeds =
240 tasks. Block execution order is randomised once, deterministically, from
a fixed `ORDER_SEED`, specifically so that any slow drift over a multi-day
run (thermal paste settling, dust accumulation, whatever) cannot correlate
with which topology or temperature happens to be scheduled early versus
late. Between blocks, `runner.Runner.settle()` holds for `BLOCK_SETTLE_S`
(300s), polled in short increments rather than slept in one long blocking
call, so that a keyboard interrupt is honoured within a second or two rather
than after up to five minutes, a five-minute uninterruptible sleep repeated
across 24 blocks would mean up to two hours in which an operator's only
option is to kill the process outright and lose whatever task was mid-flight.
Every block writes three separate CSV tables (`*.calls.csv`, one row per
model call, the unit of energy attribution; `*.tasks.csv`, one row per
graded item, carrying `correct`/`f1`/`n_calls`; `*.idle.csv`, periodic idle
power baselines, sampled with the server up and the model resident in
memory, never with the server down, because resident weights draw
non-trivial refresh power that a server-down baseline would wrongly
attribute to every subsequent call). A run is resumable by construction:
`runner.completed_tasks()` recovers already-finished (item, seed) pairs
directly from the task table already on disk, so a crash on day six costs
at most the one task that was in flight, never the six days behind it, and
`run_campaign.py --resume` refuses to continue a run under a different
configuration hash than the one it was started under, because rows written
under two different frozen-parameter sets are not statistically poolable
and must never silently end up in the same output directory.

### 5.7 Bring-up discipline: proving the pipeline before trusting a joule of it

Because the physical Jetson, the GPIO trigger line, and the INA3221 rails
did not all exist or work from day one, the whole orchestration core is
built against three abstract interfaces (`client.Trigger`, `client.Device`,
`client.EnergyMeter`) with harmless no-op default implementations
(`NullTrigger`, `NullDevice`, `NullEnergyMeter`). This is what lets
`selftest.py` exercise every piece of call-construction, retry, topology,
and record-writing logic on a laptop with no server and no hardware at all,
and what let `dry_run.py` validate the model's actual behavior (format
adherence, accuracy, topology behavior, token lengths, thinking-suppression)
against a live `llama.cpp` server before the Jetson or the power rig
existed. `run_campaign.py` refuses to start a real (non-`--dry`) run against
anything that is still structurally one of those no-op stand-ins
(`is_stub()`, checked per-method rather than per-class, because a subclass
that overrides only some of an interface's methods is a more dangerous and
more likely mistake than a subclass that overrides none of them, it would
otherwise emit real trigger pulses while silently reporting pulse-count
zero on every row, corrupting the join to the external rig while every
energy column still looks populated). `check_device.py` is the script that
answers, once on the physical Jetson, the questions that genuinely cannot be
answered from a laptop: which thermal zone name the SoC reports under this
JetPack release, what temperature range the device idles and loads at (used
to pick `THERMAL_TARGET_C`), which GPIO chip and line number are free to
claim as the trigger, and which hwmon label spelling this specific carrier
board uses for its INA3221 channels.

### 5.8 Current status and what the evidence says so far

As of this writing, Phase 1 (pipeline construction and laptop-side
verification) is essentially complete: 279 self-test checks pass with zero
failures and two known, accepted warnings (a deprecation notice on a
`llama.cpp` flag, and a note that the campaign's own statistical power only
resolves the accuracy band to about ±10 points, not tighter). Live-server
dry runs at increasing statistical power have resolved two of the three
standing methodological questions that were open earlier in the project:
`solver_critic` has been confirmed to be doing genuine iterative revision
rather than behaving as a null (flat-accept) topology, evidenced by its
per-item prompt growth matching what a real draft-plus-critique exchange
should add. `hotpotqa`'s baseline accuracy has been found to read near the
band's 70% ceiling (~65% observed) in a way that does not resolve to `IN
BAND` even at the campaign's own full statistical power, investigated and
found to be a genuine model-capability finding rather than an artifact of
easy item selection or lenient grading (both hypotheses were explicitly
checked against real evidence and ruled out; see `CHANGES.md`,
2026-08-24), and it has been decided, deliberately, to proceed without
touching grading and to document this as an acknowledged limitation of the
`hotpotqa` accuracy comparisons in the eventual write-up, while noting that
energy and cost comparisons are unaffected by it. The question of whether
`hotpotqa`'s debate answer-change rate sitting almost exactly on the 10%
null-topology floor (confirmed, not small-sample noise, at n=40
agent-rounds) was a genuine near-null finding or a prompt-engagement
problem has been decided: treated as the latter. `debate_agent.txt`'s
reconsideration instruction was rewritten to require an agent to check a
peer's specific supporting evidence before keeping or changing its answer,
rather than reasoning about the peer's answer only in the vague terms the
old wording allowed (`prompts_hash` moved from `a80170cf67250404` to
`61e6c78bad256b91`; see `CHANGES.md`, 2026-08-24, for the full rationale
and the exact wording change). This has since been verified against a live
dry run at the same sample size as the reading that flagged it:
`hotpotqa`'s debate change rate moved from 10.0% to 17.5% (7 of 40
agent-rounds, clear of the >10% floor rather than sitting on it), and
`debate`'s `hotpotqa` accuracy held at 60%, inside the target band. This
is confirmed at the sample size tested (`--topology-items 20`), not yet
re-run at higher power, see `CHANGES.md`, 2026-08-24, for the full
readout and one minor unresolved side note on `MAX_TOKENS` hits ticking
up slightly.

`planner_worker` was the last of the four topologies with no dedicated
behavioural check, only the generic accuracy/calls table every topology
gets. `dry_run.py` now tracks whether the planner produces a usable plan
or silently falls back to handing every worker the raw, undecomposed task
(`planner_worker.py`'s own documented fallback), and the first reading
against a live server found zero fallbacks on either dataset (20 of 20
items on both `gsm_hard` and `hotpotqa`; see `CHANGES.md`, 2026-08-26).
Every topology now has a dedicated check, not just an accuracy number, and
every one currently passes: `baseline` needs none, `debate` clears the
>10% floor on both datasets, `solver_critic` shows genuine revision on
both, `planner_worker` shows zero raw-task fallback on both. Nothing here
is Jetson-specific, so this closes the agentic-environment recheck on dev
hardware.

## 6. What runs where

| Component | Host | Why | Current status |
|---|---|---|---|
| `llama.cpp` server | Jetson Orin NX 8GB | Inference under measurement | working, launched routinely via `scripts/serve_dev.sh` |
| Orchestrator (`src/masenergy/`) | Jetson | Drives the GPIO trigger; must be local | implemented and self-tested; the hardware-facing half (`jetson.py`, `gpio.py`, `ina3221.py`) has never yet run against the physical Jetson itself, see Section 5.7 |
| ESP32 sampler firmware | ESP32 | Reads the external INA226, watches the trigger | not written yet; `firmware/esp32/` holds only a `.gitkeep` placeholder |
| Serial capture (`host/`) | Laptop | ESP32 is powered by and logs to the laptop | not started; no `host/` directory exists in the repository yet |
| Analysis (`analysis/`) | Laptop | Offline | not written yet; `analysis/` holds only a `.gitkeep` placeholder |

Develop on the laptop, deploy `src/` to the Jetson.

## 7. Frozen parameters

Everything below is fixed for the whole campaign; changing any one of them
mid-campaign invalidates cross-block comparability. Cross-checked here
against the actual current values in `src/masenergy/config.py`:

- Precision: native BF16/FP16. **No quantization anywhere.**
  (`config.PRECISION = "BF16"`, `config.QUANTIZATION = None`, both enforced
  by `config.validate()`.)
- Prompt caching: off (`cache_prompt: false`). (`config.CACHE_PROMPT =
  False`, enforced by `config.validate()`.)
- Concurrency: strictly one call in flight, ever. (Enforced structurally by
  the module-level `_CALL_LOCK` in `client.py`, and by `--parallel 1` in
  `config.LLAMA_FLAGS`; see Section 5.2.)
- Constrained decoding: none, retries are data. (No grammar or JSON-schema
  constraint appears anywhere in `client._payload()`.)
- Context size: one fixed value for all 12 configs. (`config.CTX_SIZE =
  3072`, one value shared by every one of the 12 (condition, temperature)
  cells returned by `config.cells()`.)
- Power mode: fixed `nvpmodel`, `jetson_clocks` locked, fan at fixed PWM.
  (`config.FAN_PWM = 255` is already fixed; `config.NVPMODEL_MODE` is, as
  of this writing, still `None`; it is one of the `REQUIRED_BEFORE_RUN`
  parameters that physical bring-up on the Jetson still has to fill in,
  which is why `config.validate()` currently blocks a real run on it.)

An earlier draft of this section pointed to `doc/MAS_Jetson_Design_Analysis.md`
for the rationale behind these choices. No `doc/` directory currently exists
in this repository (see the stale-reference note in Section 3), so that
reference cannot currently be followed; it is kept here, flagged rather than
silently dropped, until either the document is written or the pointer is
removed in a future `CHANGES.md`-logged edit.

## 8. Navigation: every file, in detail

Paths are given relative to the repository root. For every Python file,
every module-level function and every class method is described. Prompt
files are quoted in full, since their exact wording is itself part of the
experimental protocol and changing a single word changes `prompts_hash`.

### 8.1 Repository root

- **`README.md`**: this file.
- **`CHANGES.md`**: the append-only, authoritative log of every code
  change, every piece of diagnostic evidence gathered, every dated decision,
  and the current state of the self-test suite. See Section 9 below; this
  is where every change or fix made to any file in this repository, past or
  future, is recorded.
- **`requirements.txt`**: laptop-only Python dependencies: `pyserial` (ESP32
  serial capture), `numpy`/`pandas`/`scipy`/`statsmodels`/`matplotlib`
  (offline analysis), and `datasets` (HuggingFace dataset loading, used only
  by `scripts/prepare_datasets.py` and `scripts/screen_datasets.py`). The
  file's own comment states the rule it exists to enforce: `src/masenergy/`
  must never gain a dependency on anything listed here, because that code
  has to run unmodified on the Jetson's bare system Python.
- **`.python-version-note`**: records that laptop development happens under
  Python 3.14.3 in a venv, while the Jetson runs either Python 3.10
  (JetPack 6) or 3.12 (JetPack 7) system Python with no venv, and states the
  resulting constraint: `src/masenergy/` must stay written to Python
  3.10-compatible syntax (no PEP 695 type parameters, no `type` statement,
  no 3.11+-only syntax) since that is the oldest interpreter it will
  actually run under.
- **`.gitignore`**: excludes `data/raw/` (measurement output), caches, and
  virtual environments from version control.
- **`analysis/`, `firmware/esp32/`, `data/processed/`**: currently empty
  except for a `.gitkeep` placeholder each. `analysis/` is reserved for the
  offline regression/figure-generation code that will run against the
  finished campaign's data; `firmware/esp32/` is reserved for the ESP32
  sampler firmware (the external INA226 rig's counterpart to
  `src/masenergy/ina3221.py`); `data/processed/` is reserved for the output
  of whatever join/cleaning step eventually merges the external rig's
  stream with the Jetson's call records by `trigger_pulse_n`. None of the
  three currently contain working code, they are structural placeholders,
  not implemented pipeline stages, and should not be assumed to exist as
  functioning components until this section is updated to say otherwise.
- **`data/items/`**: the frozen, campaign-committed item sets
  (`items_gsm_hard.json`, `items_hotpotqa.json`), written by
  `scripts/prepare_datasets.py` and loaded by `datasets.load_items()`, which
  verifies each file's recorded SHA-256 hash against its actual content
  before use. `data/items/items_gsm8k.json` is also present but is a stale
  leftover from before the project switched from plain GSM8K to gsm-hard; it
  is not in `config.DATASETS` and is not read by anything, see the
  Standing Cautions in `CHANGES.md`.
- **`data/screen/`**: the gitignored working area for
  `scripts/screen_datasets.py`'s Stage-1 dataset screen: per-candidate item
  samples and the call-record CSVs from each screening run.
- **`data/debug/truncated/`**: populated only when `dry_run.py` is invoked
  with `--debug-truncated <dir>`; holds one plain-text file per model call
  that hit `MAX_TOKENS`, containing the exact prompt and the (cut-off)
  completion, for offline diagnosis of what the model was doing when it ran
  out of budget. See `client.py` below.
- **`data/raw/`**: gitignored; where real campaign and dry-run output CSVs
  land. Empty on a fresh clone.

### 8.2 `src/masenergy/`: the orchestration core

This package has zero third-party dependencies and is written to run
unmodified on both the laptop and the Jetson's system Python.

#### `src/masenergy/__init__.py`

Empty. Present only so `src/masenergy` is an importable package.

#### `src/masenergy/config.py`

The single source of truth for every frozen experimental parameter. Every
value that is fixed for the whole campaign lives here; nothing here is
runtime-computed except by `config.py`'s own helper functions. Parameters
not yet measured or chosen (e.g. `THERMAL_TARGET_C`, `TRIGGER_CHIP`,
`TRIGGER_LINE`, `PRICE_IN_PER_M`) are explicitly `None`, and `validate()`
refuses to let a real run start while any of them remain unset.

Notable constants: `MODEL_REPO`/`MODEL_FILE`/`MODEL_REVISION`/`MODEL_PATH`
pin the exact model artifact (Qwen3-1.7B, native BF16 GGUF, pinned to one
HuggingFace revision hash); `PRECISION`/`QUANTIZATION`/`KV_CACHE_TYPE`
enforce native precision; `LLAMA_FLAGS` fixes the `llama-server` invocation
(`--n-gpu-layers 999`, `--parallel 1`, `--no-context-shift`, `--no-mmap`,
`--no-cont-batching`, strict single-flight serving, prompt-shift disabled
rather than silently truncating an overrun prompt); `CONDITIONS` and
`TEMPERATURES` define the 4×3 topology/temperature grid; `TOP_P`/`TOP_K`/
`MIN_P`/`TYPICAL_P`/`REPEAT_PENALTY`/`PRESENCE_PENALTY`/
`FREQUENCY_PENALTY`/`MIROSTAT` pin every sampler `llama.cpp` has a default
for, explicitly, so that no sampler setting is silently supplied by the
server's own default and therefore invisible to `config_hash()` (the
comment specifically calls out `min_p`'s server default of 0.05, which
would otherwise quietly clip the very distribution tail the temperature
sweep exists to widen); `DEBATE_SHOWS_OWN_PRIOR` and
`PLANNER_WORKER_SHOWS_TASK` are the two topology-behavior flags described in
Section 5.3, deliberately left as named parameters (rather than hard-coded
into the topology files) so that the broken, pre-fix form of each topology
stays reachable and stays visible in `config_hash()` rather than being an
undocumented historical footnote.

Functions: `repo_root()` returns the repository root so paths resolve
identically on laptop and Jetson; `resolve_model_path()` returns the
absolute path to the model weights on whichever machine is running;
`cells()` returns the 12 (condition, temperature) pairs; `blocks()` returns
the 24 (dataset, condition, temperature) triples that `runner.py` actually
iterates; `calls_per_item()` returns a planning estimate of model calls per
item summed across all four conditions (using the midpoint of
`solver_critic`'s variable call count, and explicitly not counting
reprompts, since the true reprompt rate depends on temperature);
`estimated_calls()` multiplies that out across items, temperatures, seeds
and datasets for a total campaign call-count estimate; `validate()` raises
`RuntimeError` if any `REQUIRED_BEFORE_RUN` parameter is still `None`, and
separately enforces that quantization stays off, prompt caching stays off,
and thinking mode stays off, regardless of whether those three were
individually left unset; `snapshot()` collects every uppercase
module-level constant into a dict and returns it alongside a truncated
SHA-256 hash of its JSON serialization; `config_hash()` returns just that
hash, which is what gets stamped onto every `CallRecord`. Run directly
(`python3 -m masenergy.config` / `python3 src/masenergy/config.py`), the
module prints the cell/block counts, the calls-per-item and total-call
estimates, the current config hash, and whether `validate()` currently
passes or what it is blocked on.

#### `src/masenergy/chat.py`

ChatML prompt templating and frozen prompt-file loading. Templating is done
here, in Python, rather than left to the server, specifically so that the
exact token sequence sent to the model is fully authored and known by this
codebase, reporting a `prompt_n` token count for text the server itself
assembled would mean reporting a number for content this project did not
fully control.

`load(name)` reads and caches (module-level `_CACHE` dict) one prompt file
from `src/masenergy/prompts/<name>.txt`, stripped of surrounding whitespace.
`prompts_hash()` returns a truncated SHA-256 hash over every prompt file's
name and bytes, sorted, so that any change to any prompt's wording changes
this hash; it is stamped onto every `CallRecord` alongside `config_hash()`
precisely because prompt wording is an experimental parameter that lives
outside `config.py` and would otherwise be able to change the experiment
without changing its recorded identity. `build(system, user,
suppress_thinking=True)` renders one single-turn exchange as raw ChatML
text (`<|im_start|>system...`, `<|im_start|>user...`, `<|im_start|>assistant`),
and, because Qwen3 is a hybrid "thinking" model whose reasoning mode is
suppressed not by a flag but by a template detail, appends an empty
`<think>\n\n</think>\n\n` block to open the assistant turn when
`suppress_thinking` is true (the default and, per `config.THINKING_MODE`,
the only mode this study ever uses). `call_seed(base_seed, call_index)`
derives a distinct integer seed per call within one task
(`base_seed * 1000 + call_index * 10`), spaced wider than
`MAX_REPROMPTS` specifically so a retry's seed offset can never collide with
the next logical call's seed, without distinct seeds, two debate agents
given the same prompt and the same seed would emit identical text and
silently collapse the topology into one voice.

#### `src/masenergy/datasets.py`

Item loading, answer extraction, and grading, the dataset-facing half of
the pipeline described in detail in Section 5.4. Runs on the Jetson with the
standard library only; it reads the plain JSON that
`scripts/prepare_datasets.py` writes on the laptop, so the device itself
never needs the `datasets` library.

Key module-level constants: `GSM_HARD`/`HOTPOTQA` are the two dataset name
strings used throughout the codebase as the dataset identity; several
compiled regexes for locating an `"Answer:"` marker, numbers, boxed LaTeX,
and echoed template placeholders; `CONTAINMENT_FACTOR`/`CONTAINMENT_SLACK`
bound how much longer than the gold phrase a `hotpotqa` prediction is
allowed to be while still counting as "containing" it;
`NUMERIC_REL_TOL`/`NUMERIC_ZERO_TOL` set the relative-tolerance numeric
grading rule described in Section 5.4; `SPAN_GOLD_MAX_TOKENS`/
`NUMERIC_GOLD_MAX_DECIMALS` set the thresholds `gold_shape()` uses to flag
an unscoreable reference answer.

Functions, in the order they appear: `nested_sample(pool_size, n_items,
seed)` draws indices from a benchmark's row pool such that a draw of 80
items always contains the draw of 15 items taken with the same seed, this
is what lets the Stage-1 screen (15 items) and the full campaign (80 items)
be honestly described as measuring "the same thing at different sample
sizes" rather than two unrelated samples; it is implemented as a sort of a
single fixed `random.Random(seed).shuffle()` permutation's prefix, explicitly
documented as not relying on any particular incidental behavior of Python's
`random.sample`. `load_items(path)` loads a prepared item file and raises if
its recorded SHA-256 hash does not match its actual content; this is the
check that stops a campaign from silently running against an item file that
was edited after being frozen. `items_hash(items)` computes that hash from
a canonical JSON serialization. `format_number_gold(value)` renders a
numeric gold answer without a spurious trailing `.0`, shared between the
screen and the campaign preparer so the same numeric gold is stored as the
identical string in both places. `context_block(pairs)` joins a list of
(title, paragraph) tuples into the multi-paragraph context string stored on
a `hotpotqa` item. `render_context_task(context, question)` renders the
exact task string a context-bearing item becomes (`"Context:\n...\n\n
Question: ..."`), used identically by the screen and the campaign so that
a dataset is screened on the literal prompt the campaign will actually send.
`numbers_match(predicted, gold)` returns whether two numeric strings agree
within `NUMERIC_REL_TOL` (with a near-zero absolute floor via
`NUMERIC_ZERO_TOL` to avoid a division-adjacent degenerate case at a gold of
exactly zero), returning `False` rather than raising on anything
unparseable. `_strip_number(text)` removes thousands separators, dollar
signs, and a trailing period from a numeric string before parsing.
`_unwrap(text)` strips echoed template placeholders (e.g. a leaked
`<your final answer>` slot) and LaTeX/markdown display wrappers
(`$$...$$`, `\boxed{...}`, backticks, bold markers) from a candidate answer
string, on the reasoning that a template slot leaking into a response is an
instrument fault rather than a genuinely malformed answer and stripping it
recovers a measurement that was never actually the model's to get wrong.
`_first_usable(lines)` returns the first line, walked in a given order, that
unwraps to something other than pure punctuation/delimiters. `_normalise(text)`
lowercases, Unicode-normalises, strips punctuation, and drops the articles
"a"/"an"/"the" from a string before comparison, notably, this function does
*not* equate numeral and word forms of numbers (e.g. `"10"` vs `"ten"`),
which is a known, deliberately-unfixed grading gap documented in
`CHANGES.md`. `extract_answer(dataset, text)` is the main extraction
entry point described in Section 5.4: finds the last `"Answer:"` marker in
the response, and for `GSM_HARD` returns the last number found in the
marked text (falling back to the whole response if the marked segment
contains no number); for `HOTPOTQA` returns the unwrapped marked text if
present, otherwise the first usable line after the marker, otherwise the
first usable line found scanning the whole response backwards from the end;
returns `None` (a parse failure) if nothing usable is found anywhere. `_f1(
predicted, gold)` computes token-level F1 overlap between two normalised
strings, used as a secondary (non-grading) diagnostic metric on `hotpotqa`
tasks. `contains_gold(predicted, gold)` returns whether the (normalised)
gold phrase appears verbatim as a substring of the (normalised) prediction,
provided the prediction is not more than `max(CONTAINMENT_FACTOR *
len(gold), len(gold) + CONTAINMENT_SLACK)` tokens long, the anti-gaming
length bound described in Section 5.4. `span_correct(predicted, gold,
aliases=None)` accepts a prediction if it exactly matches (normalised) the
gold answer or any provided alias, or if it bounded-contains any of them.
`grade(dataset, predicted, gold, aliases=None)` is the single grading entry
point used identically by every topology: returns `None` predictions as an
ungraded parse failure; for `GSM_HARD` returns `correct` from
`numbers_match`; for `HOTPOTQA` returns `correct` from `span_correct` plus
an `f1` diagnostic. `_decimal_places(text)` counts digits after the decimal
point in the mantissa of a numeric string (correctly handling scientific
notation). `gold_shape(dataset, gold)` is the model-blind reference-quality
classifier described in Section 5.4: returns `"unparseable"` for an empty or
unparseable gold, `"over_precise"` for a `GSM_HARD` gold with more decimal
places than `NUMERIC_GOLD_MAX_DECIMALS` allows, `"long_span"` for a
`HOTPOTQA` gold longer than `SPAN_GOLD_MAX_TOKENS` tokens (a boundary case
worth noting: a gold like `"the full 24 hours"`, exactly 4 tokens after
normalising, does *not* get flagged even though a terser correct answer
could not contain that padded phrase verbatim, a known edge of this
classifier, not currently acted on), and `"ok"` otherwise. `build_task_text(
dataset, item)` renders one item's task string: the bare question for
`GSM_HARD`, or the context-plus-question block (via `render_context_task`)
for `HOTPOTQA`.

#### `src/masenergy/band.py`

The accuracy-band statistics described in Section 5.4. `BAND_LOW`/
`BAND_HIGH` fix the target band at 45.0–70.0%; `Z` is the z-score for a 95%
confidence level. `wilson(correct, total, z=Z)` computes the Wilson score
confidence interval in percent, chosen over a normal approximation
specifically because the normal approximation misbehaves at observed rates
near 0% or 100%. `verdict(correct, total, low=BAND_LOW, high=BAND_HIGH)`
returns a 4-tuple `(verdict_string, point_estimate, lo, hi)`, where
`verdict_string` is `"NO DATA"` (zero total), `"IN BAND"` (interval fully
inside the band), `"ABOVE BAND, ceiling"` (interval fully above), `"BELOW
BAND, floor"` (interval fully below), or `"UNRESOLVED, n too small"` (the
interval straddles a boundary and the sample genuinely cannot decide).
`n_for_halfwidth(halfwidth_pts, p=0.55, z=Z)` returns the sample size needed
to achieve a given half-width, evaluated at the least favourable observed
rate near 50%. `format_verdict(correct, total)` renders one line of report
text combining the point estimate, interval, raw counts, and verdict
string; this is what `dry_run.py` and `screen_datasets.py`'s reports print
per dataset/temperature cell. Run directly, the module prints the band
width and, for a range of sample sizes, the resulting Wilson interval width
at a representative 55% observed rate, plus the sample size needed for a
few representative half-widths; this is the script that was used ad hoc
during this project's investigation into whether `hotpotqa`'s near-ceiling
accuracy could ever resolve with more data (Section 5.8).

#### `src/masenergy/records.py`

The per-call record schema and the append-only CSV writer. `CallRecord` is
a `dataclasses.dataclass` with every field described inline in Section 5.1
and 3.5, identity fields (`run_id`, `config_hash`, `prompts_hash`,
`timestamp_utc`, `dataset`, `item_id`, `topology`, `temperature`, `seed`,
`repetition`), call-position fields (`role`, `round_index`,
`call_index_in_task`, `is_retry`, `retry_reason`), token counts (`prompt_n`,
`prompt_n_total`, `predicted_n`, `slot_cache_n`, kept as three separate
fields, never summed into one `total_tokens`, because the input/output
token asymmetry question the study cares about cannot be answered from a
sum), timing (`wall_clock_ms_orchestrator`, `server_prefill_ms`,
`server_decode_ms`), the trigger fields (`trigger_high_ts`,
`trigger_low_ts`, `trigger_pulse_n`, `trigger_edge_us`), the energy fields
(`energy_j_external`, always NaN in every row this pipeline itself
produces, since the Jetson has no path to the external rig's reading, and
is only ever filled in by an offline join script keyed on
`trigger_pulse_n`; `energy_j_ina_vdd_in`, `energy_j_ina_cpu_gpu_cv`,
`energy_j_ina_soc`, `idle_w_reference`), meter evidence
(`meter_samples_n`, `meter_rate_hz`, `meter_window_s`, `hw_status`),
temperature and clock state (`temp_c_soc_before`, `temp_c_soc_after`,
`temp_c_cpu`, `temp_c_gpu`, `ambient_c`, `gate_wait_s`, `gate_timed_out`,
`freq_gpu`, `freq_cpu`, `freq_emc`, `nvpmodel_mode`, `fan_pwm`), and
response/grading-adjacent bookkeeping (`finish_reason`, `truncated`,
`thinking_leak`, `parse_ok`, `answer_extracted`). `FIELDS` is the ordered
tuple of every field name, derived from the dataclass itself so the schema
can never silently drift from the class definition. `HW_FAULTS` is the
closed vocabulary of every fault token that may appear in `hw_status`
(`trigger_edge_failed`, `meter_thread_dead`, `meter_no_samples`,
`meter_rate_low`, `meter_rail_unreadable`, `meter_rail_missing`,
`soc_temp_unreadable`, `cpu_temp_unreadable`, `gpu_temp_unreadable`,
`freq_unreadable`, `nvpmodel_unreadable`, `fan_unreadable`), closed
deliberately, so that "how many rows are clean" can always be answered by
counting against a known list rather than by discovering ad hoc fault
strings scattered through the data after the fact. `hw_status(tokens)`
renders a set of fault tokens into one sorted, deduplicated, pipe-joined
cell, and raises if given any token outside `HW_FAULTS` (a typo in a fault
name is a fault that would otherwise never be counted). A module-level
guard loop immediately after `FIELDS` raises `RuntimeError` at import time
if any forbidden aggregate field name (`total_tokens`, `tokens`,
`n_tokens`) ever appears in the schema, enforcing the "never sum input and
output tokens" rule structurally rather than by convention.
`_check_header(path)` refuses to let `RecordWriter` append to an existing
CSV whose header does not exactly match the current `FIELDS` tuple, which
is what stops a schema change between two sessions of a resumed run from
silently writing correctly-formed but incompatible CSV. `new_run_id(
config_hash)` generates a `YYYYMMDDTHHMMSSZ-<config_hash>` identifier.
`utc_now()` returns the current UTC time as an ISO 8601 string.
`RecordWriter` is the append-only writer class: `__init__` opens (or
resumes) the CSV file, verifying the header if the file already existed,
and writes an initial JSON metadata sidecar file (`<name>.meta.json`);
`write(record)` appends one `CallRecord` as a row, flushing immediately
after every write (so a crash loses at most the row in flight) and calling
`os.fsync` every `fsync_every` (default 50) rows (so a power loss costs
seconds of data, not an entire block); `_write_meta()` rewrites the JSON
sidecar with the current row count; `close()` flushes, fsyncs, closes the
file handle, and stamps a `finished_utc` timestamp into the sidecar;
`__enter__`/`__exit__` make the class usable as a context manager, which is
how it is used everywhere in the codebase.

#### `src/masenergy/client.py`

Described in full in Section 5.2. `Trigger`, `Device`, and `EnergyMeter` are
the three abstract hardware interfaces, each with a harmless no-op default
implementation of every method it declares (`Trigger.high()`/`low()` return
the current wall-clock time and do nothing else; `Trigger.status()` returns
a zeroed pulse record; `Device.wait_for_gate()` returns immediately with no
wait; `Device.read_state()` returns zeroed/absent readings;
`EnergyMeter.start()`/`stop()`/`measure_idle()` return zeroed energy
readings). `NullTrigger`, `NullDevice`, `NullEnergyMeter` are empty
subclasses used explicitly for `--dry` rehearsal runs and for
`selftest.py`. `ServerError` is the exception type raised for any
malformed or error-carrying response from `llama.cpp`. `_as_cell(value)`
renders a validator's extracted value into one CSV-safe string, specifically
handling the planner topology's list-of-subtasks return value (which would
otherwise be written into the CSV as a Python `repr()` of a list, a bug this
project found and fixed). `LlamaClient` is the class every topology actually
calls into: `__init__` stores the writer, hardware interfaces (defaulting to
the Null implementations), host/port, run id, and the new
`debug_truncated_dir` option (see below), and computes `config_hash`/
`prompts_hash` once at construction. `_payload(prompt, temperature, seed)`
builds the full JSON body sent to `llama.cpp`'s `/completion` endpoint,
explicitly naming every sampler parameter `config.py` pins (see Section
8.2's `config.py` entry for why this matters). `_post(payload)` issues the
actual HTTP POST via `urllib.request` and raises `ServerError` on any
transport failure. `health()` returns whether `/health` currently answers
with HTTP 200. `_hw_status(*readings)` merges the fault sets from every
hardware reading taken during one call into the closed-vocabulary status
string, and separately tracks and periodically shouts about consecutive
faulted calls (Section 5.5). `call(prompt, temperature, seed, context)` is
the trigger-bracketed call method described in Section 5.2: acquires the
module-level `_CALL_LOCK`, gates on temperature, reads device state, starts
the meter, raises the trigger, posts the HTTP request, lowers the trigger,
stops the meter, reads device state again, validates the response is a
well-formed non-error object (raising `ServerError` otherwise, including a
specific check that a 200-status response carrying an `error` body is
treated as a hard failure rather than three wasted retries against a server
that will not recover), raises `ServerError` if prompt caching is
unexpectedly active despite `config.CACHE_PROMPT` being `False` (a live
sanity check that a stray `--cache-reuse`/`--slot-save-path` server flag
has not silently reintroduced caching), constructs and returns the full
`CallRecord`, and, if `debug_truncated_dir` is set and this particular
call's `finish_reason` came back as `"limit"` (i.e. it hit `MAX_TOKENS`), 
writes the full prompt and cut-off completion text to a timestamped file
under that directory, after the trigger has already gone low so the write
itself can never be inside a measured window. `call_with_retries(prompt,
temperature, seed, context, validator)` repeatedly calls `call()`,
re-seeding each attempt via the caller-supplied seed offset, until the
supplied `validator(text)` accepts the response or `MAX_REPROMPTS` extra
attempts have been exhausted, writing every attempt (including failed ones)
as its own record via `self.writer.write()`; this is the method every
topology actually calls, never `call()` directly.

#### `src/masenergy/jetson.py`

The real (non-Null) hardware readings, described in Section 5.7's closing
paragraph and Section 5.6. Constants: `THERMAL_ROOT`/`DEVFREQ_ROOT`/
`CPUFREQ_GLOB` are the relevant sysfs roots; `GPU_DEVFREQ_MARKERS`/
`EMC_DEVFREQ_MARKERS` are name substrings used to find the right devfreq
node by name rather than by index (device numbering is not stable across
JetPack releases); `SOC_ZONE_NAMES`/`CPU_ZONE_NAMES`/`GPU_ZONE_NAMES` are
the possible thermal-zone type names this board might report, again matched
by name for the same reason; `NVPMODEL_STATUS`/`FAN_PWM_GLOB` locate the
active power-mode file and fan PWM hwmon node.

`ThermalUnavailable` is raised when the thermal zones a gate depends on
cannot be found. `discover_zones(root)` maps every lowercased thermal zone
type name the kernel exposes to the sysfs path holding its temperature.
`read_zone_c(path)` converts a raw millidegree sysfs reading to Celsius (the
kernel reports millidegrees; reading the raw value as degrees would put
every reading a factor of a thousand too low and hold the thermal gate open
until it times out on every single call, a specific bug class this
docstring calls out explicitly). `_first_present(zones, names)` returns the
sysfs path for the first name in a priority list that is actually present.
`wait_until_in_band(read_temp, target, tolerance, timeout_s, poll_s,
monotonic=..., sleep=...)` is the actual thermal-gate polling loop described
at length in Section 5.2/5.6 (why both directions matter, why a timeout is
preferable to hanging forever); `read_temp`, `monotonic`, and `sleep` are
injected as parameters specifically so this policy logic can be exercised
by `selftest.py` with synthetic clocks and no real waiting or hardware.
`_read_hz(path)` reads a raw sysfs frequency value as an integer.
`devfreq_path(markers, root)` finds the `cur_freq` file for the first
devfreq device whose name matches one of the given markers.
`read_cpu_hz(root)` returns the *highest* current frequency across all
online CPU cores, not core 0, because the governor parks idle cores and
inference load does not spread evenly, reading only core 0 could report a
parked, idle core while another core is actually running the workload at
full clock. `read_frequencies(faults, devfreq_root, cpu_root)` returns
`(gpu_hz, cpu_hz, emc_hz)`, converting scaling_cur_freq's native kilohertz
and devfreq's native hertz into one shared unit (hertz), reading candidate
paths directly rather than checking existence first, deliberately, because
`Path.exists()` itself raises `PermissionError` when a parent directory
(such as the root-only debugfs path the EMC clock sometimes lives under)
cannot be traversed by an unprivileged user, which is exactly the case that
needs to degrade gracefully rather than crash; appends `"freq_unreadable"`
to `faults` if not every value could be read.

`JetsonTrigger(Trigger)`: `__init__(chip, line, consumer)` claims the
configured (or explicitly passed) GPIO line via `gpio.OutputLine`, raising
`gpio.GpioError` immediately if `TRIGGER_CHIP`/`TRIGGER_LINE` are still
unset in `config.py`. `_edge(value)` times and drives one edge, catching and
recording (rather than raising on) any GPIO failure. `high()`/`low()`
increment the pulse counter (on `high()`), drive the line, and record the
worse of the two edges' timing as `_edge_us`. `status()` returns the pulse
ordinal and worst edge timing, the ordinal counts pulses *attempted*, not
confirmed, so a single failed edge is flagged on its own row rather than
silently shifting every subsequent row's join index by one. `close()`
releases the line.

`JetsonEnergyMeter(EnergyMeter)`: `__init__(rails, poll_s, span_s)` discovers
(or accepts explicitly, for testing) the onboard INA3221 rails, raising
`ina3221.RailsUnavailable` if none at all could be found, and starts a
background `RailSampler` thread immediately at construction (not per-call, 
see the class docstring's explanation of why running continuously is what
lets the sampler's own CPU cost cancel out of every subtraction rather than
biasing every call's energy figure upward). `start()` records the sampler's
current monotonic clock reading as the window's start. `_window()` returns
the samples collected between that start and now. `_measure(samples)`
computes per-rail integrated energy plus a full fault list (dead sampler
thread, too few samples, observed rate below `METER_RATE_FLOOR_HZ`, missing
rail, unreadable rail), a rail this board's hwmon does not expose reports
NaN and `meter_rail_missing`, never a silent zero. `stop()` returns the
per-call energy reading dict, always with `energy_j_external` and
`idle_w_reference` as NaN (neither can be produced by anything running on
the Jetson, see Section 5.1/5.5). `measure_idle(seconds)` samples a fixed
window and returns the mean idle wattage on the `vdd_in` rail (also always
with `idle_w_external` as NaN). `close()` stops the background sampler
thread.

`JetsonDevice(Device)`: `__init__(root)` discovers thermal zones and selects
the SoC/CPU/GPU zone paths by name priority, raising `ThermalUnavailable`
immediately if no SoC zone can be found at all, deliberately, so this class
simply cannot be constructed on a machine where it would otherwise report
plausible-looking zeros. `read_soc_temp()` returns the current SoC
temperature, which is the value the thermal gate actually acts on.
`wait_for_gate()` calls `wait_until_in_band` with the live sysfs reader and
`config.py`'s frozen target/tolerance/timeout/poll values.
`_zone_or_nan(path, token, faults)` returns a zone's temperature or NaN with
the given fault token if the path is absent or unreadable. `read_state()`
assembles the full per-call device-state dict (temperatures, frequencies
via `read_frequencies`, nvpmodel mode, fan PWM, and the accumulated fault
list). `read_nvpmodel(faults)` reads the active power mode from the file
`nvpmodel` writes on every mode change, falling back to the configured value
(and flagging `nvpmodel_unreadable`) if that file cannot be read, the
fallback exists to keep the column populated, but the fault flag exists
specifically so a reader never mistakes the fallback value for a confirmed
reading of what the device was actually in. `read_fan_pwm(faults)` reads the
fan's current PWM duty cycle from the first matching hwmon node, falling
back to the configured value (flagging `fan_unreadable`) if none is found.

#### `src/masenergy/gpio.py`

Described in Section 5.2's trigger discussion. A from-scratch ctypes binding
to the Linux GPIO character-device v2 ABI (`/dev/gpiochip*`), chosen over
the deprecated `/sys/class/gpio` interface specifically because the latter
costs a full open/write/close syscall sequence per edge with latency that
varies with page-cache state, whereas the character-device ioctl interface
lets a line be claimed once at construction and driven with a single ioctl
per edge on an already-open descriptor. `GpioError` is the module's
exception type. `_ioc(direction, nr, size)` encodes an ioctl request number
exactly the way the kernel's `_IOC` macro does. `_LineValues`,
`_LineAttributeValue`, `_LineAttribute`, `_LineConfigAttribute`,
`_LineConfig`, `_LineRequest`, `_ChipInfo`, `_LineInfo` are `ctypes`
structure/union definitions mirroring the kernel's `gpio_v2_*` and
`gpiochip_info` structs field-for-field, including explicit padding.
`GPIO_GET_CHIPINFO_IOCTL`, `GPIO_V2_GET_LINEINFO_IOCTL`,
`GPIO_V2_GET_LINE_IOCTL`, `GPIO_V2_LINE_SET_VALUES_IOCTL` are the four
ioctl request numbers computed via `_ioc()` from those structures.
`ABI_SIZES`, `ABI_OFFSETS`, `ABI_REQUESTS` are dictionaries recording the
expected byte sizes, expected field byte-offsets, and expected raw ioctl
request-number values that a correct 64-bit build of these structures must
produce, checked, not assumed, because two structures can have the
identical total size while a field has silently shifted position (the
module's own docstring gives the concrete example: dropping
`event_buffer_size` from the request struct leaves `sizeof()` unchanged at
592 bytes, because the four bytes are reclaimed by trailing alignment
padding, while every field after the hole silently shifts and the kernel
ends up reading the line number out of the wrong bytes). `abi_mismatches()`
checks the live `ctypes` structures against `ABI_SIZES`/`ABI_OFFSETS`/
`ABI_REQUESTS` and returns a list of every discrepancy found, empty means
the ABI on this Python/platform build matches what the kernel expects.
`chip_paths(root)` lists every `/dev/gpiochip*` device present.
`chip_info(path)` ioctls a chip's kernel name, board label, and line count.
`line_info(path, offset)` ioctls one line's name, current consumer (the
field that tells you whether a line is already claimed by another driver, 
critical during bring-up, since attempting to claim an already-held pin
would otherwise fail at the worst possible moment rather than during
bring-up inspection), and input/output direction flags. `OutputLine` is the
class actually used to drive the trigger: `__init__(chip, line, consumer,
initial)` first calls `abi_mismatches()` and refuses to proceed at all if
the ABI does not match (rather than risk driving a pin through a
misaligned payload), then issues the `GPIO_V2_GET_LINE_IOCTL` request to
claim the line as an output and stores the returned file descriptor.
`set(value)` drives the line via one `GPIO_V2_LINE_SET_VALUES_IOCTL` ioctl
on the already-open descriptor, the entire hot path, allocating nothing
and touching no filesystem. `high()`/`low()` are convenience wrappers around
`set(1)`/`set(0)`. `close()` drives the line low *before* releasing the
descriptor (low first, deliberately, because the external sampler reads a
held-high line as a call still in progress, a process that exits with the
line left high leaves an unterminated pulse, which is a recoverable
data-quality issue, whereas one that leaves it high forever is not).
`__enter__`/`__exit__` make the class usable as a context manager.

#### `src/masenergy/ina3221.py`

The onboard software energy-measurement path, described in Section 5.1's
discussion of the two independent measurement paths. Reads the Jetson's
onboard INA3221 power monitor through the kernel's `hwmon` sysfs interface,
continuously (not started/stopped per call, same "always running so its
own cost cancels" argument as `JetsonEnergyMeter`, explained again here at
the sampling-thread level).

`HWMON_ROOT` is `/sys/class/hwmon`. `RAIL_ALIASES` maps each of the three
record field suffixes this study cares about (`vdd_in`, `cpu_gpu_cv`,
`soc`) to the several label spellings actually observed across different
Orin carrier board revisions and JetPack releases. `RAIL_KEYS` is that
mapping's key tuple. `NAN` is the module's canonical NaN constant.
`RailsUnavailable` is raised when no hwmon device exposes any rail this
study records. `_read_int(path)` reads and parses one sysfs integer value.
`_canonical(label)` maps a raw kernel-reported channel label onto one of
the three field-suffix keys, or `None` if the label matches no known alias.
`Rail` is one discovered channel: `__init__` stores its key, label, and
either a (voltage path, current path) pair or a power path, preferring
voltage×current when both are available specifically because their hwmon
units (millivolts, milliamps) are unambiguous per the sysfs ABI, whereas
the power node's documented microwatt unit has been observed reported in
milliwatts by some vendor drivers, a silent factor-of-a-thousand error
that would look entirely plausible on a plot, which two extra cheap reads
avoids. `watts()` returns instantaneous power, computed either as V×I/1e6
or P/1e6 depending on `source`. `describe()` returns a one-line
human-readable summary of the rail's mapping. `discover_rails(root)` scans
every `hwmon*` device's `in*_label` files, maps each recognized label to a
`Rail` via `_canonical()`, and returns a dict of the (at most three) rails
found. `unmatched_labels(root)`, a bring-up-time diagnostic, returns
every hwmon channel label on the board that `_canonical()` does not
recognize, so a rail spelled differently than any current alias shows up as
a one-line fix to `RAIL_ALIASES` rather than a silent, permanently-missing
column. `integrate(samples, index)` computes trapezoidal energy in joules
over a list of `(t_monotonic, w0, w1, ...)` tuples for one rail index,
returning NaN (rather than a partial figure) if fewer than two samples
exist or any sample in the window is unreadable, deliberately, because a
window that silently lost half its samples would otherwise produce an
energy figure that is too small by an unknown amount while looking exactly
like a genuinely quiet call, which is precisely the failure mode this whole
module exists to make visible instead. `mean_watts(samples, index)`
computes the time-weighted mean power over a window (used for the idle
baseline), as `integrate(...) / span`. `observed_rate_hz(samples)` returns
the actually-achieved sampling rate across a window, used to detect a
starved sampler thread via `config.METER_RATE_FLOOR_HZ`. `RailSampler` is
the background sampling thread: `__init__(rails, poll_s, span_s, clock,
sleep)` sizes a bounded ring buffer (`collections.deque`) from the longest
call the server is allowed to take, so a full window can always be
reconstructed even for a call that ran all the way to the server timeout;
`clock`/`sleep` are injected for testability. `start()` launches the daemon
sampling thread. `_loop()` is the thread body: repeatedly reads every rail's
instantaneous wattage (recording NaN and incrementing `read_failures` on any
`OSError`/`ValueError`), appends a timestamped sample tuple, and sleeps
`poll_s`. `alive()` reports whether the thread is currently running.
`window(t0, t1)` returns every buffered sample whose timestamp falls
inclusively within a monotonic time range; this is called from `stop()`
*after* the trigger has already gone low, so no scan of this buffer ever
happens inside a measured window. `close()` signals the thread to stop and
joins it.

### 8.3 `src/masenergy/topologies/`: the four measured conditions

#### `src/masenergy/topologies/__init__.py`

`REGISTRY` maps each of the four topology name strings to its module's
`run` function. `get(name)` looks a topology up by name, raising `KeyError`
on an unknown name; this is the only way any other module in the codebase
ever dispatches to a topology, so adding a fifth topology means adding one
line here.

#### `src/masenergy/topologies/baseline.py`

`run(client, task, temperature, seed, ctx, validator)`, one call to the
`baseline_solver` prompt, via `call_with_retries`, tagged
`topology="baseline"`, `role="solver"`. Returns
`{"answer", "parse_ok", "n_calls", "records"}`, the return shape every
topology shares.

#### `src/masenergy/topologies/debate.py`

`run(client, task, temperature, seed, ctx, validator)` implements the
symmetric multi-round peer debate described in Section 5.3: iterates
`config.DEBATE_ROUNDS` rounds, and within each round iterates
`config.DEBATE_AGENTS` agents strictly serially (parallel only in what each
agent is *shown*, never in when it actually runs, per Section 5.2); round
one shows each agent only the bare task; later rounds construct a `peers`
string from every other agent's most recent answer and, governed by
`config.DEBATE_SHOWS_OWN_PRIOR`, either fold in the agent's own previous
answer explicitly or omit it (the pre-fix, currently-unused behavior); after
all rounds, builds a summary of every agent's final answer and issues one
more call to the `debate_synthesiser` prompt to produce the topology's
single final answer. Every call is tagged with `role="agent_<n>"` or
`role="synthesiser"` and the correct `round_index`.

#### `src/masenergy/topologies/solver_critic.py`

`_VERDICT` is the compiled regex used to find a `Verdict: ACCEPT`/`Verdict:
REJECT` line. `_verdict_validator(text)` is the custom validator passed to
`call_with_retries` for critic calls specifically (rather than the
dataset's normal answer validator), since a critic call is not producing a
task answer at all. `run(client, task, temperature, seed, ctx, validator)`
implements the asymmetric solver/critic feedback loop described in Section
5.3: one initial solver draft, then up to `config.SOLVER_CRITIC_MAX_ITERS`
iterations of critic review followed by (if rejected and iterations remain)
a solver revision that is shown the critic's entire response text, not
merely the extracted verdict. Loop exits early the moment a critic verdict
is `ACCEPT`.

#### `src/masenergy/topologies/planner_worker.py`

`_NUMBERED` is the compiled regex matching a numbered-list line (`"1. ..."`
or `"1) ..."`). `_plan_validator(text)` extracts numbered subtask lines and
rejects the plan (returns `False, None`) unless at least
`config.PLANNER_WORKER_SUBTASKS` were found. `run(client, task, temperature,
seed, ctx, validator)` implements the hierarchical delegation topology
described in Section 5.3: one planner call (using `_plan_validator`), a
fallback to `[task] * PLANNER_WORKER_SUBTASKS` if planning failed after
retries, one call per worker (each tagged `role="worker_<n>"`, shown its
subtask plus, governed by `config.PLANNER_WORKER_SHOWS_TASK`, the full
original problem as background context, and explicitly never shown any
other worker's subtask or answer), and one final synthesiser call combining
every worker's result into the topology's answer. Returns the shared
`{"answer", "parse_ok", "n_calls", "records"}` shape plus an extra
`"plan_ok"` key recording whether the planner's own output actually parsed.

### 8.4 `src/masenergy/prompts/`: the frozen role prompts

Every prompt below is quoted in full; the exact wording is part of the
experimental protocol; `chat.prompts_hash()` changes if a single character
here changes.

- **`baseline_solver.txt`**: used by the `baseline` topology's only call:

  > You are solving a problem.
  >
  > Work through the problem step by step, showing your reasoning in full.
  >
  > Then end your response with a final line that starts with "Answer:"
  > followed by the answer alone: a number, a name, or a short phrase, with
  > no restatement of the question and no explanation on that line.
  > Brevity applies only to that final line. The reasoning above it should
  > be as long as the problem needs.

- **`solver.txt`**: used by `solver_critic`'s solver role (both the initial
  draft and every revision):

  > You are solving a problem.
  >
  > If you are given a critic's feedback on a previous attempt, address
  > every point it raises before answering again.
  >
  > Work through the problem step by step, showing your reasoning in full.
  >
  > Then end your response with a final line that starts with "Answer:"
  > followed by the answer alone: a number, a name, or a short phrase, with
  > no restatement of the question and no explanation on that line.
  > Brevity applies only to that final line. The reasoning above it should
  > be as long as the problem needs.

- **`critic.txt`**: used by `solver_critic`'s critic role:

  > You are checking another solver's attempt at a problem. Decide whether
  > the final answer is correct.
  >
  > Identify any specific error in the reasoning or arithmetic. Be
  > concrete: name the step that is wrong and why. If the answer is
  > correct, say so without inventing faults.
  >
  > Finish your response with a line in exactly this form:
  > Verdict: ACCEPT
  > or
  > Verdict: REJECT

- **`debate_agent.txt`**: used by every `debate` peer-agent call:

  > You are one of several independent problem solvers working on the same
  > task.
  >
  > When you are shown another solver's answer, engage with the specific
  > fact, quoted detail, or calculation step behind it, not just their
  > final line. If their answer differs from yours, find the exact piece
  > of evidence or working that supports theirs and check it directly
  > against the source material or your own arithmetic. Change your
  > answer only when you can point to a specific error in your own prior
  > evidence or working; otherwise keep your answer and state exactly
  > which piece of the peer's supporting detail is wrong, missing, or
  > insufficient. Do not restate your own answer without addressing
  > theirs, and do not adopt theirs without checking it first.
  >
  > Work through the problem step by step, showing your reasoning in full.
  >
  > Then end your response with a final line that starts with "Answer:"
  > followed by the answer alone: a number, a name, or a short phrase, with
  > no restatement of the question and no explanation on that line.
  > Brevity applies only to that final line. The reasoning above it should
  > be as long as the problem needs.

  As of `prompts_hash 61e6c78bad256b91` (2026-08-24), the reconsideration
  paragraph was rewritten from a vaguer "if their reasoning is better,
  change your answer" instruction to an evidence-grounded one, specifically
  to address `hotpotqa`'s debate answer-change rate sitting on the
  >10% null-topology floor (see Section 5.8 and `CHANGES.md`,
  2026-08-24). The old wording is not reproduced here; see version control
  or `CHANGES.md`'s Hash history for it. Verified against a live dry run
  the same day: `hotpotqa`'s change rate moved from 10.0% to 17.5% at the
  same sample size, and `debate`'s `hotpotqa` accuracy stayed inside the
  target band; see `CHANGES.md`'s Standing Cautions and the 2026-08-24
  verification entry for the full readout.

- **`debate_synthesiser.txt`**: used by `debate`'s final combining call:

  > You are combining the final answers of several independent solvers
  > into one answer. Weigh their reasoning and decide which is correct.
  > You may pick one, or give a different answer if all of them are wrong.
  >
  > Work through the problem step by step, showing your reasoning in full.
  >
  > Then end your response with a final line that starts with "Answer:"
  > followed by the answer alone: a number, a name, or a short phrase, with
  > no restatement of the question and no explanation on that line.
  > Brevity applies only to that final line. The reasoning above it should
  > be as long as the problem needs.

- **`planner.txt`**: used by `planner_worker`'s planner role:

  > You are breaking a problem into independent subtasks for other workers
  > to solve.
  >
  > Write exactly two subtasks. Each must be answerable on its own,
  > without knowing the answer to the other, and each must be a complete
  > question containing all the information needed to answer it.
  >
  > Give them as two numbered lines, the first starting with "1." and the
  > second with "2.", each line containing only the subtask question.
  > Write nothing else.

- **`worker.txt`**: used by every `planner_worker` worker call:

  > You are answering one part of a larger problem. Answer only the
  > subtask you are given.
  >
  > Work through the problem step by step, showing your reasoning in full.
  >
  > Then end your response with a final line that starts with "Answer:"
  > followed by the answer alone: a number, a name, or a short phrase, with
  > no restatement of the question and no explanation on that line.
  > Brevity applies only to that final line. The reasoning above it should
  > be as long as the problem needs.

- **`planner_synthesiser.txt`**: used by `planner_worker`'s final combining
  call:

  > You are combining the results of several subtasks into the answer to
  > the original problem. Use the subtask results to reach the final
  > answer.
  >
  > Work through the problem step by step, showing your reasoning in full.
  >
  > Then end your response with a final line that starts with "Answer:"
  > followed by the answer alone: a number, a name, or a short phrase, with
  > no restatement of the question and no explanation on that line.
  > Brevity applies only to that final line. The reasoning above it should
  > be as long as the problem needs.

Note that six of the eight prompts (every role except `critic` and
`planner`) share an identical closing "Answer:" instruction block verbatim, 
`scripts/verify_fixes.py` and `scripts/selftest.py` both contain a
regression check (`test_prompt_surface`) that this shared block really is
byte-identical across every prompt that claims to share it, since a silent
divergence there would mean extraction is being tested against wording the
model was never actually shown.

### 8.5 `scripts/`: bring-up, verification, and the campaign entry point

#### `scripts/config.py` reference note

Not a separate file, `scripts/` imports `masenergy.config` directly via
`sys.path` manipulation at the top of every script (`ROOT = ...; sys.path
.insert(0, str(ROOT / "src"))`), which is why every script below can `from
masenergy import config` despite living outside `src/`.

#### `scripts/selftest.py`

The whole-pipeline self-test: exercises everything the campaign depends on
that can be checked with no server, no network, and no hardware, and its
exit status is the number of failed checks. `RESULTS` is the module-level
list every check appends to. `check(section, label, ok, detail="")`
appends a hard pass/fail result. `warn(section, label, ok, detail="")`
appends a soft "hygiene" result (a `WARN`, not a `FAIL`) for a check whose
failure should not by itself block a run. `StubWriter` is a fake
`RecordWriter` that collects rows in memory instead of touching disk.
`StubClient(LlamaClient)` subclasses the real client but replaces `_post()`
with a version that returns scripted canned text (default `"reasoning\n
Answer: 42"` if the script list is exhausted), while still exercising the
real record construction, the real `_CALL_LOCK`, and the real retry loop, 
deliberately not mocking those away, so what is under test is genuine
orchestration behavior against canned model output, not a reimplementation
of the client. `answered(value)`/`verdict(value)` are small text-template
helpers for building scripted stub responses. Module-level fixture data:
`EXTRACTION` (extraction test cases, every case is a string shape actually
observed during the Stage-1 screen, per the file's docstring, not an
invented one), `GRADING_SPAN`/`GRADING_NUMERIC` (grading test cases for each
dataset's rule), `HOTPOT_ROW`/`GSM_HARD_ROW` (representative raw benchmark
rows for testing the item-builder functions), `_NUMBERED`/`PLAN_SAMPLE`
(planner-parsing test fixtures), `ANSWER_PROMPTS` (the six prompts expected
to share the identical answer-format closing block).

The 28 test functions, each named `test_*` and each appending to `RESULTS`
via `check`/`warn`: `test_extraction()` runs every `EXTRACTION` case through
`datasets.extract_answer` and checks the result matches expectation.
`test_grading()` runs every `GRADING_SPAN`/`GRADING_NUMERIC` case through
`datasets.span_correct`/`numbers_match`. `test_gold_shape()` checks
`datasets.gold_shape()`'s classification boundaries, including the known
`SPAN_GOLD_MAX_TOKENS` edge case described in Section 8.2's `datasets.py`
entry. `test_prompt_surface()` checks that every `ANSWER_PROMPTS` file
shares the identical closing instruction block and that no prompt file
still contains a raw `<...>` template placeholder. `test_seed_spacing()`
checks `chat.call_seed()`'s spacing guarantee holds across the actual
`MAX_REPROMPTS` value. `test_screen_campaign_agreement()` checks that
`datasets.nested_sample()` really does nest (a smaller draw's indices are a
subset of a larger draw's, for the same seed) and that the screen's and
campaign's task-rendering functions produce byte-identical prompts for the
same underlying row. `test_topologies()` runs every one of the four
topologies against a `StubClient` with scripted responses and checks call
counts, seed spacing, role/round labelling, and (for debate specifically)
that `DEBATE_SHOWS_OWN_PRIOR` actually changes what an agent is shown.
`test_payload_and_cache()` checks `_payload()`'s sampler-field completeness
and the prompt-cache mismatch guard in `call()`. `test_debug_truncated_capture()`
(added this session) checks the `debug_truncated_dir` feature end to end:
that a call whose `finish_reason` is `"limit"` writes a file when the option
is set, that it writes nothing when unset, that a non-`"limit"` finish
writes nothing even when the option is set, and that the written file
contains the actual prompt and completion text, five checks total,
verified via deliberate mutation testing (temporarily loosening the write
guard and confirming exactly the expected checks then fail) during this
project's own development, per `CHANGES.md`. `test_band()` checks
`band.wilson()`/`band.verdict()`/`band.n_for_halfwidth()` against known
values and boundary cases. `test_serialisation()` checks that every
`CallRecord` field round-trips through CSV writing/reading correctly,
including edge-case values (NaN, booleans, negative numbers).
`test_records()` checks `RecordWriter`'s header-mismatch guard, fsync
cadence, and metadata sidecar behavior. `test_run_refuses_while_unvalidated()`
checks that `runner.Runner.run()` genuinely refuses to proceed while
`config.validate()` would fail; this is the test that exists specifically
because, historically, nothing on the execution path actually called
`validate()` and a campaign could have started with `THERMAL_TARGET_C` still
`None` (see `CHANGES.md`). `test_entry_point_guards()` checks
`run_campaign.py`'s guard logic (`is_stub`, `check_hardware`, `resolve_run`'s
resume-hash check) directly. `test_thermal_gate()` checks
`jetson.wait_until_in_band()`'s policy against injected fake clocks and
temperature sequences, including both above-band and below-band waiting and
the timeout path. `test_jetson_sysfs()` checks `jetson.py`'s sysfs parsing
functions (`discover_zones`, `read_zone_c`, `read_frequencies`,
`devfreq_path`) against a synthetic thermal-zone directory tree built in a
temp directory. `test_block_settle()` checks `runner.Runner.settle()`'s
polling behavior and interrupt responsiveness. `test_full_campaign_and_resume()`
runs a complete miniature campaign end to end against a `StubClient` and a
temp directory, interrupts it partway, and checks that resuming recovers
exactly the completed tasks and finishes the rest without redoing them.
`test_gpio_abi()` checks `gpio.abi_mismatches()` returns empty against the
module's own structure definitions (i.e. that the structures are
self-consistent) and that deliberately corrupting a structure's field is
detected. `test_rail_discovery()` checks `ina3221.discover_rails()`/
`unmatched_labels()` against a synthetic hwmon sysfs tree. `test_energy_integration()`
checks `ina3221.integrate()`/`mean_watts()`/`observed_rate_hz()` against
hand-computed trapezoidal-integration examples, including the NaN-propagation
behavior on short or faulty sample windows. `test_meter_faults()` checks
`JetsonEnergyMeter`'s fault detection (dead thread, too few samples, low
rate, missing/unreadable rail) end to end. `test_sysfs_permissions()`
specifically checks the `PermissionError`-on-traversal case in
`read_frequencies()` degrades to a fault flag rather than crashing; this is
the regression test for the `Path.exists()` bug found and fixed earlier in
the project (see `CHANGES.md`). `test_hw_status_column()` checks
`records.hw_status()`'s sorting, deduplication, and its raise on an unknown
fault token. `test_trigger_pulse_join()` checks `JetsonTrigger`'s pulse
counting and edge-failure flagging. `test_runner_end_to_end()` checks
`runner.Runner.run_block()` writing all three CSV tables (calls, tasks,
idle) with mutually consistent content against a `StubClient`.
`test_items_and_config()` checks `config.py`'s `cells()`/`blocks()`/
`calls_per_item()`/`estimated_calls()`/`snapshot()`/`config_hash()` against
hand-computed expectations. `test_context_budget()` (defined in the file,
computing worst-case prompt+output token totals per topology against
`config.CTX_SIZE` via a `_longest_task_tokens()` helper and `CHARS_PER_TOKEN`
estimate) is present in the file but is **not currently included** in
`main()`'s list of test functions to run; it exists in the source but does
not execute as part of a normal `python3 scripts/selftest.py` invocation.
This is noted here as an accurate description of the file's current state,
not a change made to it; whether to wire it back in is a decision for
`CHANGES.md`, not something silently done as part of writing this
document. `main()` runs every wired-in test function in a fixed order,
prints every recorded result grouped by section, prints the current
`config_hash`/`prompts_hash`/dataset list under a "PROVENANCE" heading, and
returns the number of failed checks as the process exit status (0 = every
check passed).

#### `scripts/dry_run.py`

Model-level go/no-go checks against a live `llama.cpp` server, answering the
five questions listed in its own module docstring and described in Section
5.7: does the prompt format elicit a parseable answer and does adherence
degrade with temperature; is baseline accuracy inside the target band; do
debate agents genuinely change their answers between rounds; does every
topology actually do the thing it is named after, including whether
`planner_worker`'s planner produces a usable plan or silently falls back to
the raw task; and what are the real token lengths (informing `CTX_SIZE`).
It also checks that Qwen3's thinking
mode is genuinely suppressed, since that is a silent failure mode
specifically at high temperature. `TOPOLOGY_TEMPERATURE` (0.7) is the fixed
temperature the four-topology sweep (as opposed to the baseline-only
temperature sweep) runs at. `pct(part, whole)` is a small percentage-format
helper. `_spread(items, n)` returns an evenly strided subsample of a frozen
item list (never a prefix, since `prepare_datasets.py` stores items sorted
by source row index, so a prefix would cluster in the low-index region of
the benchmark rather than being representative), and, notably, silently
returns the entire item list unchanged once `n >= len(items)`, meaning
`--items` (or `--topology-items`) above 80 is a no-op against these
particular frozen item files, not a request for more data; this is
documented explicitly because it was the source of a real piece of
confusion earlier in the project (see `CHANGES.md`, "why --items above 80
does nothing"). `run(n_items, n_topology_items, out_dir, port,
debug_truncated_dir=None)` is the actual driver: connects to the server
(exiting with an instructive message if none answers), then for each
dataset in `config.DATASETS`, runs the baseline topology across
`_spread(items, n_items)` at every configured temperature (populating the
accuracy-band report), and separately runs all four topologies across
`items[:n_topology_items]` at `TOPOLOGY_TEMPERATURE` (populating the
topology-behavior, debate-answer-change, solver-critic-revision-growth, and
planner-worker-plan-fallback reports), `n_items` and `n_topology_items` are
two genuinely independent
sample-size knobs, sized by two separate CLI flags (`--items` and
`--topology-items`), specifically because conflating them was a second
real, documented source of confusion in the project's history: raising
`--items` alone changes which items land in a `--topology-items`-sized
slice of them, without changing the actual statistical power behind any
topology-level conclusion. `report(calls_path, accuracy, topology_stats,
debate_answers, critic_traces, planner_plan_ok, n_topology_items)` prints
the seven numbered report sections (plus two unnumbered per-topology
addenda folded into section 4) read from a completed dry run's call-record
CSV: (1) format adherence by temperature and overall retry rate; (2)
baseline accuracy per dataset/temperature via `band.format_verdict`, plus a
worked explanation of how wide the Wilson interval is at the sample size
actually used and how many items would be needed for tighter half-widths;
(3) debate answer-change rate per dataset, comparing each agent's round-1
vs round-2 answer and flagging `*** NULL TOPOLOGY RISK ***` below 10%; (4)
a per-(dataset, topology) table of accuracy, calls, and prompt/output
tokens per item, plus two addenda: a solver-critic-specific check that a
revision's prompt actually grew by roughly as much as the draft-plus-critique
that was folded into it (flagging `*** CRITIQUE NOT REACHING SOLVER ***` if
not), and, added this session, a planner-worker-specific check of how many
items the planner produced a usable, well-formed plan for versus how many
silently fell back to `planner_worker.py`'s raw-task fallback (flagging
`*** PLANNER FELL BACK TO RAW TASK ***` on any shortfall, since a planner
that keeps missing its own format is quietly turning delegation into N
redundant attempts at the whole task); (5) token length percentiles per
dataset and whether `CTX_SIZE` leaves any headroom over the worst observed
prompt+output, plus counts of calls that hit `MAX_TOKENS` or had their
prompt truncated by the server; (6) whether any response leaked a raw
`<think>` tag; (7) the `gold_shape()` distribution over every frozen item,
i.e. how many points of accuracy are structurally unreachable because of
the reference answers alone, independent of model quality. The `__main__`
block defines the CLI: `--items` (default 10, sizes the baseline temperature
sweep), `--topology-items` (default 5, sizes the four-topology sweep,
independent of `--items`), `--port`, `--out`, and `--debug-truncated <dir>`
(added this session, see `client.py`'s entry above; off by default, zero
behavioral effect on any measurement when unset, and its writes happen
strictly after the trigger has gone low).

#### `scripts/check_device.py`

First contact with the physical Jetson, described in Section 5.7's closing
paragraph, the script that answers the questions bring-up genuinely cannot
answer from a laptop. `sample(device, seconds, interval)` polls
`device.read_soc_temp()` at a fixed interval over a fixed duration and
returns the `(elapsed, celsius)` series. `report_zones(root)` lists every
thermal zone the kernel exposes and its current reading, or an explanatory
message if none exist (e.g. because this is not actually a Jetson, or
thermal sysfs is not mounted). `report_selection(device)` prints which zone
each of soc/cpu/gpu actually resolved to, so an accidentally wrong sensor
choice is visible immediately. `report_state(device)` prints the full
per-call device-state dict and warns if any frequency reads as 0 (a value
that means the sysfs path was not found, not that the clock is actually
stopped). `report_stability(readings)` computes and prints the min/median/
max/spread and sample-to-sample standard deviation of a temperature series,
and prints a worked explanation of what the observed range implies for
choosing `THERMAL_TARGET_C` (must be reachable from both the idle floor and
the loaded ceiling) and warns if the idle drift already exceeds
`THERMAL_TOLERANCE_C` (meaning a band narrower than the sensor's own noise
could never be held, and every call would wait out the gate timeout).
`report_gate(device, target)` temporarily overrides `config.THERMAL_TARGET_C`
and runs the actual `wait_for_gate()` policy against the live device,
printing the wait time and whether it timed out. `report_gpio()` lists every
GPIO chip present and every line on it that is currently free (not already
claimed by another driver); this listing is what bring-up reads to choose
`TRIGGER_CHIP`/`TRIGGER_LINE`, since those values are device-tree properties
of the specific carrier board and cannot be guessed from a laptop.
`report_rails()` lists every INA3221 channel `ina3221.discover_rails()`
found, its current wattage, and any hwmon label present on the board that no
current alias recognizes (a one-line fix to `RAIL_ALIASES` if found).
`report_frequencies()` prints which devfreq/sysfs path resolved for GPU,
EMC, and CPU frequency reading, and how many CPU cores are reporting.
`main(argv=None)` wires up the CLI (`--gpio`, `--rails`, `--freq`,
`--sample`, `--interval`, `--gate`, `--root`), runs the requested reports in
order, and returns a nonzero exit status if `jetson.JetsonDevice` cannot
even be constructed on this machine (the correct, intended result when run
somewhere that is not actually a Jetson).

#### `scripts/diagnose.py`

A large (820-line), still-functional historical forensic tool: re-reads
every call-record table this repository has ever produced under `data/raw`
and `data/screen`, re-grades every recorded answer under several different
rules, and reports discrepancies as `BLOCKER`/`MAJOR`/`NOTE` findings. This
is the tool that, earlier in the project's history, originally surfaced and
led to the fixes for the debate own-prior bug, the planner-worker
task-visibility bug, an overly strict numeric-tolerance grading bug, and
confusion between the `truncated` and MAX_TOKENS-`finish_reason` columns
(all pre-dating this README's writing and logged in `CHANGES.md`).
`finding(severity, title, detail)` records one diagnosed defect at severity
`BLOCKER`, `MAJOR`, or `NOTE`. `h1(title)`/`h2(title)` print section
headers. `pct(part, whole)` is the shared percentage-format helper.
`read_runs()` loads every call table found under `data/raw` and
`data/screen`, oldest first. `load_item_index()` builds an `item_id -> item`
lookup spanning both the frozen campaign item sets and the screening item
sets. `baseline_tasks(rows)` groups baseline call rows into per-task attempt
sequences, newest attempt last. `final_answer(attempts)` and `gold_of(item)`
are small accessors. `grade_shipped(dataset, predicted, item)` re-applies
today's actual grading rule (i.e. `datasets.grade`), the reference point
every other regrading rule is compared against. `grade_absolute(dataset,
predicted, item)` re-grades under the repository's *previous* rule (an
absolute 1e-6 numeric tolerance), to show what accuracy the old, since-fixed
grader would have reported on the same data. `grade_either_direction`,
`grade_f1`, and `grade_loose_numeric` are three deliberately looser
"diagnostic ceiling" rules (accepting a sub-span match in either direction,
an F1-overlap threshold, and a looser three-significant-figure numeric
tolerance respectively) used only to characterize how much of an accuracy
gap is attributable to grading strictness versus genuine model error, never
used to actually re-grade the shipped data. `section_selftest(brief)` runs
`selftest.py`'s checks inline and reports the result as part of the
diagnosis. `section_power(brief)` reports the Wilson-interval statistical
power available at the sample sizes actually run so far. `section_gold_audit
(brief)` reports the `gold_shape()` distribution across every frozen item,
same purpose as `dry_run.py`'s section 7 but computed directly from the item
files rather than from a dry-run's CSV. `section_regrade(runs, items,
brief)` re-grades every recorded answer under every rule above and reports
how much the accuracy figure moves. `section_failures(runs, items, brief)`
surfaces concrete examples of what the model's actual misses look like.
`_topology_stats(runs)` computes debate/solver_critic/planner_worker health
statistics (answer-change rate, revision prompt growth, etc.) across every
run found on disk. `_is_current_build(rows)` flags whether a given run's
rows were written under the current record schema (i.e. after the fixes
this script itself historically drove), so pre-fix and post-fix runs are
not silently pooled together in a diagnosis. `section_topologies(runs,
brief)` reports topology health using `_topology_stats`.
`section_instrumentation(runs, brief)` checks recorded-row-level
instrumentation consistency (e.g. the `truncated` vs `finish_reason=="limit"`
distinction). `section_screen_vs_campaign(brief)` and
`_check_draw_matches_preparer()` check that the committed item files still
match the exact draw `prepare_datasets.py`'s sampling logic would produce
today, i.e. that nothing about the frozen sets has silently drifted from
what the screen originally validated. `section_verdict(brief)` prints the
tool's overall diagnosis, synthesizing every section above.
`_wrap(text, width)` is a text-wrapping helper for terminal output.
`main()` runs every section in order (all sections, or a `--brief`
subset per its CLI, with an optional `--strict` mode) and returns a
nonzero exit code if any `BLOCKER`-severity finding was raised. `row(label,
num, den, stats_pair)` is a small table-row formatting helper used
throughout the report sections.

#### `scripts/verify_fixes.py`

A regression check for the extraction/grading behavior, predating this
project's rename of the numeric-reasoning dataset from `gsm8k` to
`gsm_hard`. **This script is currently broken and does not run**: its
module-level line `GSM8K, HOTPOTQA = ds.GSM8K, ds.HOTPOTQA` raises
`AttributeError: module 'masenergy.datasets' has no attribute 'GSM8K'` the
moment it is imported or executed, because `datasets.py` now exports
`GSM_HARD`, not `GSM8K`, confirmed in this session by actually running
`python3 scripts/verify_fixes.py` and observing the traceback, not merely
inferred from reading the source. Every case this file's `EXTRACTION` and
`GRADING` lists cover is now duplicated, updated to the current names, and
actively run inside `scripts/selftest.py`'s `test_extraction()` and
`test_grading()`. The file is kept in the repository as a historical
artifact of the rename rather than deleted, but should be treated as dead
code: `main()` (which would print pass/fail tables for its `EXTRACTION` and
`GRADING` fixture lists, a planner-parse check, and a prompt-surface check
identical in spirit to `selftest.py`'s versions) never actually executes,
because the module fails at import time before `main()` is ever reached.
Anyone extending this project's extraction or grading logic should update
`selftest.py`, not this file.

#### `scripts/prepare_datasets.py`

Downloads, samples, and freezes the two campaign item sets. Laptop-only, 
imports the `datasets` library lazily, specifically so the row-building
functions themselves stay unit-testable without a network connection.
`OUT_DIR` is `data/items/`. `GSM_HARD_REPOS`/`GSM_HARD_SPLIT`/
`HOTPOTQA_REPOS`/`HOTPOTQA_SPLIT` list the candidate HuggingFace repo names
and split to load each dataset from (a list rather than one name, since a
dataset can be hosted under more than one repo path). `_load(names, subset,
split)` tries each candidate repo name in order and raises with every
attempt's error if none load. `_sample_indices(n_total, n_want, seed)` is a
thin wrapper around `datasets.nested_sample`, kept as a separate function so
the screen and the campaign preparer are visibly calling the exact same
shared sampling logic. `build_gsm_hard(row, index, rank)` renders one raw
gsm-hard row into the campaign's item schema (`id`, `rank`, `question`,
`answer` via `format_number_gold`, `context: None`, `level: None`), the
docstring notes explicitly this is gsm-**hard**, not plain gsm8k, and why
that distinction is what puts this dataset inside the accuracy band.
`gold_pairs(row)` extracts the supporting (non-distractor) paragraphs from a
raw hotpotqa row. `build_hotpotqa(row, index, rank)` renders one raw
hotpotqa row into the campaign's item schema, including the assembled
context block and the row's difficulty `level` field (used later in this
project's investigation into whether `hotpotqa`'s near-ceiling accuracy was
an artifact of easy item selection, see Section 5.8). `prepare_gsm_hard(
n_items, seed)` and `prepare_hotpotqa(n_items, seed)` each load their full
source split, sample `n_items` indices via `_sample_indices`, and return the
built item list alongside provenance (source repo name, total pool size).
`write(name, items, source, pool_size, split, seed, force=False)` freezes
one item set to `data/items/items_<name>.json` with a recorded SHA-256 hash
and provenance metadata, and, unless `--force` is passed, refuses to
overwrite an existing file whose content would actually change, printing an
explicit warning that blocks already run were measured against the
currently-recorded set and that overwriting must be a deliberate act, not
an accidental one from re-running this script. `main(force=False)` prepares
and writes both datasets using `config.N_ITEMS` and `config.ORDER_SEED`. The
`__main__` block exposes a `--force` CLI flag.

#### `scripts/screen_datasets.py`

The Stage-1 candidate screen described in Section 5.4: runs the baseline
topology, at one fixed temperature, over a small sample of each of six
candidate datasets, and reports where each one lands relative to the
accuracy band, before any dataset is committed to the frozen campaign item
files. `SCREEN_DIR` is `data/screen/` (gitignored). `_num(value)` and
`_paragraph_block(pairs, question)` delegate to `datasets.format_number_gold`
and `datasets.render_context_task` respectively, specifically so the screen
renders numbers and context-bearing tasks identically to how the campaign
eventually will. `_gold_pairs(row)` extracts supporting paragraphs from a
raw hotpotqa-shaped row (duplicated from, but kept independent of,
`prepare_datasets.gold_pairs` since the screen operates on more dataset
shapes than the campaign preparer does). `_keep_all`, `_keep_math500`,
`_keep_drop`, `_keep_wiki`, `_keep_musique` are per-candidate row-admission
filters (e.g. `_keep_math500` keeps only difficulty levels 1–3; `_keep_drop`
requires a non-empty answer span; `_keep_musique` requires the row to be
answerable and have at least one supporting paragraph). `_build_gsm_hard`,
`_build_math500`, `_build_drop`, `_build_hotpotqa`, `_build_wiki2hop`,
`_build_musique` render one raw row of each respective candidate dataset
into the screen's own item schema (`id`, `task`, `answer`, `aliases`, and
for math500 also `level`). `_BOXED`/`_TEXT_WRAP` are regexes and `_norm_math
(text)` is a MATH-500-specific normalizer that strips LaTeX boxing,
`\text{}`/`\mbox{}`/`\mathrm{}` wrappers, spacing commands, degree symbols,
percent signs, and whitespace before comparison. `_grade_numeric(predicted,
gold, aliases)` delegates to the real campaign grader (`datasets.grade`)
rather than reimplementing numeric comparison, the docstring notes this
replaced an earlier, looser relative-tolerance rule the screen used to use
on its own, which had been silently accepting answers the campaign's actual
grader would reject. `_grade_math(predicted, gold, aliases)` combines
`_norm_math` exact matching with a numeric fallback, for MATH-500.
`_grade_span(predicted, gold, aliases)` delegates to `datasets.span_correct`
for the same reason `_grade_numeric` delegates to the real grader.
`GRADERS` maps grader-name strings to these three functions. `CANDIDATES` is
the tuple of six candidate-dataset specifications (name, prompt "slot"
category, grader, extraction-format alias, source repo list, subset, split,
row filter, row builder) that every other function in this file iterates
over. `_load(repos, subset, split)` mirrors `prepare_datasets._load`.
`_validator(kind)` builds an extraction validator for a given
`extract_as` dataset-format alias. `_selected(only)` filters `CANDIDATES` to
just the names passed via `--only`, or all of them if none were specified.
`prep(n_items, only)` downloads, filters, samples (via the same
`nested_sample` the campaign preparer uses, so a 30-item screen is
guaranteed to contain the exact 15-item screen taken at the same seed), and
writes each selected candidate's items to `data/screen/items_<name>.json`.
`run(n_items, port, only, temperature)` runs the baseline topology over
every prepared candidate's items against a live server and collects
per-dataset correct/total counts. `report(calls_path, summary, temperature)`
prints the per-candidate accuracy/parse-rate/token-length table with each
dataset's `band.verdict()`, plus overall retry, MAX_TOKENS, prompt-truncation,
and thinking-leak counts, and a worked note on how wide the Wilson interval
is at the screen's sample size and how many items would be needed to narrow
it. The `__main__` block wires up `--prep`, `--run`, `--items` (default 15),
`--temperature` (default 0.7), `--port`, and `--only` (a comma-separated
candidate-name filter), requiring at least one of `--prep`/`--run`.

#### `scripts/find_model.py`

A small standalone utility (uses the `huggingface_hub` library) for locating
a native-precision (BF16/F16/FP16, never a `Q4`/`Q5`/etc. quantized file)
GGUF build of the configured model. `CANDIDATE_REPOS` builds a list of
likely GGUF-hosting repo names derived from `config.MODEL_REPO`.
`NATIVE` lists the precision-tag substrings this script accepts. `main()`
queries each candidate repo via `HfApi.repo_info`, lists every native-
precision `.gguf` file found (with size), lists what non-native files exist
instead when no native file is found in a repo, and finally checks that the
original source (non-GGUF) model repo exists as a fallback for local
conversion via `llama.cpp`'s own `convert_hf_to_gguf.py --outtype bf16`.

#### `scripts/serve_dev.sh`

A bash launcher for a local development `llama-server`, explicitly labelled
in its own header comment as producing no energy number that means
anything; it exists purely so the rest of the pipeline (dry runs,
self-tests-against-a-live-server, day-to-day development) has something to
talk to. Reads `CTX_SIZE`, `SERVER_PORT`, and `KV_CACHE_TYPE` from
`config.py` via a `PYTHONPATH`-driven `python3 -c` call, rather than
hard-coding them, specifically so a value read here can never silently
diverge from the value `config_hash()` actually records. Refuses to launch
against a model filename containing `Q4`/`Q5`/`Q6`/`Q8` (any case), this
study is native precision only, and a quantized weights file passed by
accident is refused at launch rather than served with no warning. Reads
`LLAMA_FLAGS` the same `PYTHONPATH` way, via a `read` loop rather than
bash's `mapfile`, because the script needs to run identically under macOS's
still-shipped bash 3.2 (`mapfile` is a bash-4+ builtin) as often as under
the Jetson's newer bash. Prints the resolved model, context size (with an
explicit warning if serving at a context size other than `config.CTX_SIZE`,
since the only legitimate reason to do that is screening a candidate
dataset whose prompts are longer than the campaign's own context), and
resolved flags, then `exec`s `llama-server` with `--model`, `--ctx-size`,
`--port`, `--cache-type-k`/`--cache-type-v` (from `KV_CACHE_TYPE`), and the
full `LLAMA_FLAGS` list appended.

#### `scripts/run_campaign.py`

The campaign entry point described in Section 5.6/5.7, the only supported
way to start or resume a real measurement run, and, per its own module
docstring, structured entirely as a sequence of gates: by the time `run()`
is actually reached, every frozen parameter is set, a server is answering,
the item files on disk still match their recorded hashes, and the hardware
objects in hand have been shown to be something other than the no-op stand-
ins this repository ships by default. `STUB_METHODS` maps each of the three
hardware base classes to the tuple of method names that must be overridden
for an instance to count as a genuine (non-stub) implementation
(`Trigger`: `high`, `low`, `status`; `Device`: `wait_for_gate`,
`read_state`; `EnergyMeter`: `start`, `stop`, `measure_idle`, deliberately
excluding `close()` from every list, since `close()` is lifecycle rather
than measurement, and a real implementation with genuinely nothing to
release is entitled to simply inherit the no-op). `is_stub(instance, base)`
returns whether *any* (not *all*) of a base class's measured methods are
still the exact ones inherited from the base, checked with `any()`
specifically because a partially-implemented interface (say, a `Trigger`
subclass that drives real GPIO edges in `high()`/`low()` but forgot to
override `status()`) is judged the harder and likelier accident: such an
object would emit real trigger pulses on real hardware while still reporting
pulse-index zero on every single row, silently corrupting the join to the
external rig while every energy column still looks fully populated, an
`all()` check would let exactly that object through. `hardware(dry)`
returns the `(trigger, device, meter)` triple a run will actually be
measured through: the three `Null*` implementations, deliberately, under
`--dry`; otherwise the three real `jetson.Jetson*` implementations,
constructed and, if any of them raises its own unavailability exception
(`ThermalUnavailable`, `RailsUnavailable`, `GpioError`), closes whatever was
already successfully built and exits with an instructive message directing
the operator to `--dry` for a rehearsal instead. `check_hardware(trigger,
device, meter)` returns the list of interface names (if any) that
`is_stub()` still considers stub implementations. `resolve_run(resume, dry)`
returns the run id and output directory for this invocation: a fresh,
timestamped run id normally; the exact `resume` id if `--resume` was passed,
after checking that its trailing config-hash suffix matches the *current*
`config.config_hash()` (refusing to resume a run that was started under a
different configuration, since rows written under two different frozen
parameter sets could never be legitimately pooled); and, under `--dry`, a
`rehearsal-<run_id>` output folder specifically so a rehearsal run can never
land in the same directory a real measurement would use. `banner(runner,
dry, host, port)` prints everything needed to identify a run before it
writes a single row: run id, config hash, prompts hash, each dataset's item
file hash and item count, server address, output directory, block/task/
estimated-call counts, and (under `--dry`) an explicit reminder that every
energy and temperature column will read zero. `main(argv=None)` wires up
the CLI (`--dry`, `--resume`, `--port`, `--host`), calls `config.validate()`
first and exits with an explanatory message if it fails (noting explicitly
that `--dry` does *not* exempt a run from this check, since rehearsing under
a configuration the real campaign could not even use is not actually
rehearsing the campaign), builds and stub-checks the hardware trio (exiting
if any are still stubs and this is not a `--dry` run), resolves the run id
and output directory, constructs the `LlamaClient` and checks server health,
constructs the `Runner` and prints the banner, runs the campaign (closing
every hardware instrument in a `finally` block regardless of how the run
ends), and finally prints the exact `--resume` command line that would
continue this run if it stopped early.

## 9. Changes and fixes

Every change made to any file in this repository, every bug fix, every new
feature, every piece of diagnostic evidence gathered about the data already
on disk, and every deliberate decision about how to handle a finding (up to
and including a decision to *not* change something, and why), is recorded
in **`CHANGES.md`**, in reverse-chronological order, with the rationale for
the change, its blast radius (which files and which prior results it
affects), and, where applicable, the evidence that the change actually
fixed what it claimed to fix. `CHANGES.md` also carries the running total of
`selftest.py`'s check count and pass/fail status, a hash-history table
tracking every value `config.config_hash()` has taken across the project's
history, and a "Standing Cautions" section listing every currently open
question or known, deliberately-unaddressed limitation. If a file's current
behavior in this README ever appears to disagree with the file's actual
behavior on disk, `CHANGES.md` is the authoritative record of what changed
and when, this README describes the repository as of the date it was
written, `CHANGES.md` is the append-only history of how it got there and
where it is still headed.