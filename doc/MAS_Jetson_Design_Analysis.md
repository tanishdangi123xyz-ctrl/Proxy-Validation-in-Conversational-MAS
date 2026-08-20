# Conversational MAS on Jetson Orin NX — Design Analysis

**Hardware:** Orin NX 8GB, exclusive and continuous access

---

## ⚠ Check this before anything else

Orin NX 8GB has ~6–6.5 GB usable after OS and services, and there is a known JetPack-level blocker at exactly the size you need:

**On JetPack R36.4.7 and earlier, CUDA IOVA allocation fragmentation prevents models larger than roughly 1 GB from loading at all on 8GB Orin modules.** It is reported fixed in **JetPack 6.2.2**. A 3B Q4_K_M is ~2 GB, so on the wrong JetPack your entire model shortlist fails to load and it will look like a llama.cpp problem rather than a platform one.

**Verify your JetPack version first.** If it's below 6.2.2, flashing is step zero — and it has to happen before any calibration, because it changes the memory allocator behaviour you'd be characterising.

---

## 0. The framing that should drive every decision

This is not an agent-building project that happens to be measured. It is a **measurement project that happens to need agents**. Every design choice below is made in service of one requirement: that the energy number attached to a call is unambiguously that call's, and that nothing in the software stack silently changes what a "token" or a "call" costs.

That inversion matters because the obvious way to build a debate/solver-critic/planner-worker system (grab AutoGen, run agents concurrently, let the server cache prefixes, constrain output to JSON) breaks the experiment in four separate places. Each one is covered below.

---

## 1. What the briefing fixes, and what is still open

**Fixed:** platforms, 4 conditions × 3 temperatures = 12 configs, both measurement paths on NX, NVML on A100, 6-week timeline.

**Still open, and all of it has to be decided before code:**

| Decision | Why it can't be deferred |
|---|---|
| Which SLM, which quantization, pinned revision | Sets throughput → sets how many items you can afford |
| Task / dataset | Never specified in the brief; determines whether planner-worker is a real topology or a fake one |
| Serving stack and its exact flags | Caching and batching defaults will silently invalidate the token proxy |
| Concurrency policy | Determines whether trigger-line attribution is valid at all |
| Structured-output policy | Determines whether the temperature-as-cause question is answerable |
| Retry policy and cap | Retries are the mechanism under study, not an implementation detail |
| Definition of "monetary cost" on hardware you own | There is no dollar figure on a Jetson |
| Item count and repetitions | Determines statistical power of the proxy regressions |

---

## 2. Four constraints that the measurement imposes on the agent code

### 2.1 Strict global serialisation — exactly one call in flight, ever

Debate's round 1 is described as agents answering "in parallel." That is a statement about **information flow** (no agent sees another's answer), not about time. If two calls overlap on the wall clock, the external sampler sees one blended current trace and there is no way to split it. The trigger line cannot encode two overlapping intervals.

On a single Orin GPU you lose nothing: concurrent requests time-share the same SMs, so parallelism buys throughput only through batching, which you are about to disable anyway.

**Do:** one global lock around the call. `call()` is the only place an HTTP request is issued, and it is mutually exclusive.

**Report:** state in the methods that "parallel" denotes independence of conditioning, and that all calls are executed serially. This is also a scope limit on your wall-clock finding — wall-clock as a proxy is being validated *for serialised execution*, and a reviewer will ask, so pre-empt it.

### 2.2 The server stays resident; idle is measured with the model loaded

Loading a 3B model is several GB off NVMe plus allocation. If the model loads per call, load energy dominates and you are measuring I/O.

Less obvious: **the idle baseline you subtract must be measured with the server up and the weights resident.** Weights sitting in LPDDR5 draw refresh power, and the server process holds CUDA context. If you subtract "device with nothing running," you will attribute the model's static residency cost to every call, inflating all of them by a constant — which happens to be exactly the kind of constant offset that inflates R² and makes proxies look better than they are.

**Do:** persistent server, one model load per block, idle measured server-up-and-idle immediately before and after each block, plus short idle windows interleaved between calls so you can model idle drift rather than assuming it flat.

### 2.3 Prompt caching off

llama.cpp's server enables `cache_prompt` by default. Debate round 2's prompt shares a long prefix with round 1. With caching on, prefill for that call is largely skipped — so two calls with identical `prompt_n` can differ in energy by an order of magnitude depending on what ran before them.

That is not a property of the debate topology. It is a property of your server config, and it would show up in your results as "the token proxy fails," which would be a wrong conclusion drawn for a right-looking reason.

**Do (primary design):** `cache_prompt: false` per request, `--parallel 1`, continuous batching off, `--no-context-shift`. Every call is self-contained so that "input tokens" means the same physical work every time.

**Optional secondary arm, if time allows:** rerun a subset with caching on. "Serving-layer prefix caching decouples token count from energy" is a clean, small, separate finding that strengthens the paper. Do not mix it into the primary 12.

### 2.4 No constrained decoding (GBNF / JSON schema)

This is the one most likely to be gotten wrong, because constraining output is normally just good practice.

GBNF grammars mask invalid tokens **before** sampling. Format-failure rate drops to ~0 regardless of temperature. Research question #2 is whether temperature affects energy *causally, through retry frequency*. Constrained decoding deletes the mechanism you are trying to observe.

**Do:** free-form generation, lenient regex/heuristic parsing, and treat every parse failure as a **first-class logged event with its own energy**, followed by a re-prompt. Cap re-prompts (2 is reasonable) and log when the cap is hit — at temp 1.0 with a 3–4B model, runaway loops will happen.

Retries are your data. Do not engineer them away.

---

## 3. Model choice

### 3.1 Selection criteria, in priority order for *this* experiment

1. **Fits with real headroom.** Any swap to NVMe puts storage I/O inside your measurement boundary. On 8GB this is the binding constraint, not a formality — see the memory budget in §3.4.
2. **Fast enough for the schedule** (see §4 — you have far more slack than you'd guess).
3. **Format adherence that *moves* with temperature.** You want reliable-ish at 0.2 and visibly degrading at 1.0. A model that never fails, or always fails, gives you no variance on the mediator.
4. **Non-reasoning / thinking mode off.** A hybrid thinking model's output length varies by an order of magnitude call to call. That variance will swamp your topology effect and burn your statistical power on nothing.
5. **Dense transformer.** Not MoE (routing makes energy-per-token depend on which experts fire — genuinely interesting, but it's a different paper). Not hybrid SSM/attention (LFM2.5-class): Jetson CUDA kernel maturity for those is uneven, and you'd be measuring kernel quality, not coordination structure.
6. **Permissive licence, pinned revision hash.** Pin the exact GGUF file. Six weeks is long enough for a repo to be re-uploaded under you.

### 3.2 No quantization — and what that costs you

**Decision: run at native BF16/FP16. No quantization anywhere in the design.**

This is the right call and I'd argue for it even if you hadn't. Your review's own §3 makes compression a *contested* variable — de Reus et al. found quantization increasing energy, Husom et al. found it halving energy, and the reconciling factor is hardware support. If you run the whole experiment at Q4_K_M, every finding carries an asterisk: *"proxy validity in conversational MAS, at 4-bit."* A reviewer who has read your own review paper will immediately ask whether the result is a topology finding or a quantization finding, and you would have no way to separate them. Running native removes the question entirely.

**First, a clarification that saves you a stack rewrite: GGUF is not a quantization format.** GGUF is a container, and it supports `F16` and `BF16` tensor types with no quantization applied. You keep llama.cpp and everything in §6 unchanged — you just convert or download the F16/BF16 GGUF instead of a `Q*` one. "No compression" and "llama.cpp" are not in tension.

**Now the cost.** Native precision doubles bytes-per-parameter, and on 8GB that reaches straight into your model ceiling:

| Model | Params | **FP16 weights** | Fits in ~6.0–6.5 GB usable? |
|---|---|---|---|
| Llama-3.2-3B | 3.2B | **~6.4 GB** | ❌ weights alone exceed budget |
| Qwen3-4B | 4.0B | **~8.0 GB** | ❌ not close |
| Phi-4-mini | 3.8B | **~7.6 GB** | ❌ not close |
| Qwen3-1.7B | 1.7B | ~3.4 GB | ✅ |
| Llama-3.2-1B | 1.24B | ~2.5 GB | ✅ comfortable |
| Gemma-3-1B | 1.0B | ~2.0 GB | ✅ comfortable |

**The 3–4B class is gone.** Unquantized on an 8GB Orin NX, your ceiling is roughly **1.7B**. That is the real cost of the constraint, and it's worth being explicit that you are trading model capability for methodological cleanliness. I think that trade is correct for *this* paper — your contribution is about coordination structure, not about how well a model reasons — but it introduces a risk covered in §3.3.

### 3.3 Revised recommendation

**Primary: Qwen3-1.7B (BF16, thinking disabled). Robustness arm: Llama-3.2-1B (FP16).**

| Model | Case for | Case against |
|---|---|---|
| **Qwen3-1.7B** | Best reasoning-per-byte that fits unquantized; strong multi-turn for its size; dense transformer, no exotic kernels | Hybrid thinking must be firmly disabled **and verified to stay off at temp 1.0** — this is a silent failure mode that would wreck your output-length distribution |
| **Llama-3.2-1B** | Very comfortable memory margin; plain architecture; already a substrate in the energy literature, so your numbers can be sanity-checked externally | Weakest reasoning of the three — real risk of degenerate debate |
| **Gemma-3-1B** | Smallest footprint; sliding-window attention keeps KV small | Interleaved local/global attention makes KV accounting less uniform across context lengths |

Two different families is deliberate: "the proxy relationship holds across Qwen and Llama at native precision" is a materially stronger claim than a single-model result, and §4 shows you can afford both.

**The risk you are accepting, and how to check it early.** At 1.7B, will two agents in a debate genuinely reconsider, or just restate? If the answer-change rate between rounds is near zero, debate is a *null topology* — you'd be measuring the energy cost of agents talking past each other, and the paper's central comparison weakens badly.

This is a **go/no-go pilot check, not a nice-to-have** (calibration task #2 in §11): run 20 items through debate and measure what fraction of agents change their final answer between round 1 and round 2. If it's under ~10%, you have a problem, and the fix is task difficulty (§5), not a bigger model — you no longer have room for a bigger model.

**If a 16GB Orin NX is procurable within the six weeks, that single change unlocks 3B at FP16** (~6.4 GB weights + ~0.9 GB KV + buffers ≈ 7.8 GB, comfortable in ~14 GB usable) and removes the only real weakness in this design. Worth asking the lab before you commit to 1.7B.

### 3.4 Memory budget on 8GB — the arithmetic that decides the model

Usable after OS and services: **~6.0–6.5 GB**. KV cache is a larger fraction of your budget than people expect, and it scales with the `--ctx-size` you fix in §6.

Per-token KV at FP16:

| Model | Layers | KV heads × dim | KV/token | KV @ 4096 | KV @ 8192 |
|---|---|---|---|---|---|
| Qwen3-1.7B | 28 | 8 × 128 | ~112 KiB | ~0.46 GB | ~0.92 GB |
| Llama-3.2-1B | 16 | 8 × 64 | ~32 KiB | ~0.13 GB | ~0.27 GB |

Full footprint at **native precision**:

| Config | Weights | KV @ 4096 | Buffers | **Total** | Verdict |
|---|---|---|---|---|---|
| **Qwen3-1.7B BF16** | ~3.4 GB | 0.46 GB | ~0.4 GB | **~4.3 GB** | ✅ comfortable |
| Qwen3-1.7B BF16 @ 8192 ctx | ~3.4 GB | 0.92 GB | ~0.5 GB | **~4.8 GB** | ✅ workable |
| **Llama-3.2-1B FP16** | ~2.5 GB | 0.13 GB | ~0.3 GB | **~2.9 GB** | ✅ lots of margin |
| Llama-3.2-3B FP16 | ~6.4 GB | 0.46 GB | ~0.5 GB | **~7.4 GB** | ❌ over budget |

**Both recommended models fit at native precision with real margin.** Size `--ctx-size` from your measured worst-case debate context (§11 step 5) — 4096 is likely sufficient and buys you headroom over 8192 for free.

**Do not reach for KV cache quantization** (`--cache-type-k q8_0`) to buy room. Beyond being compression you've excluded on principle, it changes numerics as a function of context length — and context length varies systematically by topology, since debate has the longest prompts. That would be a confound correlated with your independent variable, which is the worst kind. Keep KV at FP16 and pay for it with a smaller context.

**One counterintuitive consequence worth putting in the paper.** Native precision makes decode *slower*, not faster, despite the smaller parameter count — because decode is memory-bandwidth bound and what matters is bytes, not parameters:

| | Bytes | Theoretical ceiling @ 102 GB/s |
|---|---|---|
| Llama-3.2-3B Q4_K_M | 2.0 GB | ~51 tok/s |
| Qwen3-1.7B BF16 | 3.4 GB | ~30 tok/s |

A model with roughly *half* the parameters is roughly *40% slower* to decode. That is a clean, self-contained illustration of your review's own argument that model size is a poor proxy — and you'll have measured it on your own hardware, in passing, as a byproduct of a design decision. Worth a paragraph.

### 3.5 One sentence you still need in the methods

Even running native, say explicitly: *"All models were run at native BF16/FP16 precision. No quantization, pruning, or distillation was applied at any stage."* Reviewers who know the compression literature will look for this, and its absence reads as an omission rather than a choice. One sentence closes the entire line of questioning.

---

## 4. Schedule math — and the conclusion that falls out of it

### 4.1 How the estimate is constructed

It is a bottom-up product of six parameters. Every one is currently an **assumption**, and the point of the week-1 calibration in §11 is to replace each with a **measurement** before you commit to an item count:

```
total_time  =  N_items × N_temps × Σ(calls_per_task) × N_seeds
               × ( t_prefill + t_decode + t_cooldown + t_overhead )
```

| Parameter | Current value | Basis | Confidence |
|---|---|---|---|
| `t_decode` | ~17 tok/s | 102 GB/s ÷ 3.4 GB = 30 tok/s ceiling, × ~55–60% real-world efficiency | Medium — **measure** |
| `t_prefill` | ~400 tok/s | Compute-bound; 8GB NX has lower GPU clock (~765 MHz) and ~70 TOPS | Low — **measure** |
| output tokens/call | ~250 | Capped at 512; assumed typical | Low — **measure in pilot** |
| prompt tokens/call | ~900 worst case | Task + two round-1 answers + instructions | Low — **measure in pilot** |
| `calls_per_task` | ~14.5 | Structural for debate; *variable* for solver-critic and planner-worker | Low — **measure in pilot** |
| `t_cooldown` | 10–40s | Thermal time constant, entirely unmeasured | **Lowest — measure first** |

Per call at the current assumptions: 900 tokens prefill at ~400 tok/s = 2.3s; 250 tokens decode at ~17 tok/s = 14.7s → **~17s of inference.**

Calls per item, per temperature:

| Condition | Calls | Fixed or variable? |
|---|---|---|
| Baseline | 1 | Fixed |
| Debate (2 agents × 2 rounds + synthesis) | 5 | Fixed by design |
| Solver-critic (cap 4 iterations) | ~4 avg | **Variable** — depends on critic accept rate, which rises with temperature |
| Planner-worker (1 plan + 2–3 workers + 1 synth) | ~4.5 avg | **Variable** — depends on how many subtasks the planner emits |
| **Total** | **~14.5** | |

Note that two of the four are variable, and both vary *in the direction of your independent variable* — higher temperature should mean more solver-critic iterations. So this estimate is a floor, and the pilot needs to measure the iteration distribution at temp 1.0 specifically, not just at 0.2.

150 items × 3 temps × 14.5 calls × 3 seeds ≈ **19,600 calls**.

### 4.2 Cooldown is the dominant term — which is why it drives the schedule

At 17s inference per call, the cooldown gate is not a rounding error, it's *most of your wall clock*:

| Cooldown per call | Total wall clock | Days continuous |
|---|---|---|
| 10s | ~147 h | ~6.1 |
| 20s | ~201 h | ~8.4 |
| 30s | ~256 h | ~10.7 |
| 40s | ~310 h | ~12.9 |

Add ~15% for idle-measurement blocks, server restarts, failed calls, and re-runs.

**Every one of these fits in six weeks for one model**, and two models fits comfortably up to ~20s cooldown.

**But the schedule is explicitly not a constraint on this project.** The table above is context, not a budget — its only real use is telling you roughly when to check back. Nothing in the design should be chosen to make a number in it smaller. See §4.4.

**If you ever do need levers:** drop to 2 seeds (−33%) or 120 items (−20%). Never shorten the cooldown, and never drop a temperature or a topology — those are the design.

### 4.4 What "time doesn't matter" actually buys

Since the schedule is free, spend it on accuracy. Ranked by value per hour spent:

| # | Upgrade | Cost | What it buys |
|---|---|---|---|
| 1 | **Per-call idle bracketing** — measure idle immediately before *and* after every call, not once per block | ~2× wall clock | Turns idle subtraction from a block-average *assumption* into a *local measurement*. Removes the largest remaining modelling assumption in the pipeline. Biggest single accuracy win available. |
| 2 | **Reference-call canary** — re-run one fixed, identical call every ~100 calls for the whole campaign | ~1% | A drift detector spanning the entire run. If the canary's energy moves, something changed — sensor, room, thermal interface, device. Without it, six-day drift is invisible and uncorrectable after the fact. Best value in the table by a wide margin. |
| 3 | **Tighter thermal band** — ±0.5 °C instead of ±2 °C | Longer gates | Directly shrinks the residual thermal confound that §8.1 exists to remove. |
| 4 | **5 seeds instead of 3** | +67% | Better within-condition variance estimates — which matter specifically because "energy variance rises with temperature" is one of your results, and variance needs more samples than means do. |
| 5 | **Replicate the campaign in reversed block order** | 2× | Detects order effects and slow drift directly, rather than trusting randomisation to have handled them. |
| 6 | **Mid-campaign INA226 recalibration**, not just start and end | ~1 hour | Three calibration points bound drift far better than two, and let you interpolate a correction rather than merely report a bound. |
| 7 | **Longer warm-up discards** after every server start | Minutes per block | Removes first-call allocation and page-fault artifacts outright rather than hoping N was large enough. |

Items 1 and 2 together roughly double the run and are worth it. **Do both.**

Item 2 deserves emphasis: the canary is the only mechanism in this design that can tell you *after the fact* whether a six-day dataset is internally consistent. Everything else assumes stability. The canary tests it.

### 4.3 The conclusion

**You are not compute-limited. You are thermally and instrumentation-limited.** The GPU work itself is ~90 hours; everything else in the budget is waiting for silicon to settle and for instrumentation to be trustworthy. Spend the surplus, in descending order of value:

1. **Run the full 12 configs on a second model.** The single strongest use of the slack. "The proxy relationship holds/breaks across two independently trained models at native precision" is a materially stronger claim than a single-model result.
2. **More items (150–200) and 3 seeds.** ~20k call-level observations is comfortable for mixed-effects regression with item as a random effect.
3. **Baseline-only at a second model size.** Cheap (~1/14th of a run) and gives you a reference for "how much does model size move the proxy relationship versus how much does topology" — useful for arguing your finding isn't a quirk of one model.

Note that repetitions at temp 0.2 will be near-identical while temp 1.0 varies a lot. That is not wasted — within-condition energy variance as a function of temperature is itself a reportable result.

---

## 5. The task/dataset — the biggest unspecified thing in the brief

The briefing never says what the agents reason *about*. This matters more than it appears, because the three topologies have different requirements:

- **Debate** needs a defensible answer that a peer can actually argue you out of → verifiable QA / arithmetic. (Du et al. 2024 used exactly this.)
- **Solver-critic** needs a task where a critic can name a *specific* error → verifiable answers.
- **Planner-worker** needs a task that genuinely decomposes into **independent** subtasks. This is the binding constraint. GSM8K decomposes into sequential *steps*, not independent subtasks, so a planner-worker on GSM8K is somewhat artificial and a reviewer will say so.

### 5.1 The governing principle: calibrate difficulty to the model

This matters much more now that §3.2 has capped you at 1.7B. **You want baseline single-call accuracy in roughly the 45–70% band.** The reason is structural, not aesthetic:

- **Near ceiling (>90%):** the solver is right first time, so the critic always accepts on iteration 1, debate agents never change their answers, and the planner's decomposition is irrelevant. All three topologies collapse behaviourally into the baseline. You'd measure their *overhead* but nothing about coordination — and any proxy-validity finding would be about the cost of redundant calls.
- **Near floor (<20%):** the model produces noise, the critic critiques noise, debate agents converge on nothing. You measure degeneration.
- **In the band:** the solver is genuinely sometimes wrong, so critics catch real errors, debate agents genuinely flip, and iteration counts vary meaningfully with temperature — which is exactly the mechanism RQ2 needs.

So the dataset is chosen *against the model*, not in the abstract. A 1.7B on MATH-500 sits near the floor; on ARC-Easy it sits near the ceiling. Neither gives you an experiment.

### 5.2 Recommendation

**GSM8K as primary, HotpotQA (gold-paragraph setting) as secondary.**

| | Why | Fit for 1.7B |
|---|---|---|
| **GSM8K** | Canonical debate benchmark (Du et al. 2024), so your debate results are comparable to the literature the review cites. Exact-match numeric grading. The critic has something concrete to check — an arithmetic slip is a *nameable* error, which is what makes solver-critic non-vacuous. | Qwen3-1.7B should land in the target band. **Verify in the pilot.** |
| **HotpotQA**, gold paragraphs supplied in-prompt | The only one of the candidates where planner-worker is genuinely real: a multi-hop question decomposes into **independent** sub-questions that workers can answer without each other's output. | Supplying the gold paragraphs turns it from closed-book recall (which a 1.7B fails) into reading comprehension (which it can do). Keeps it conversational — no retrieval, no tool calls. |

**The trade on HotpotQA:** supplying paragraphs inflates prompt length substantially, which raises prefill cost, KV footprint, and therefore your `--ctx-size` floor. Budget for it when you size context in §11 step 5. It also *improves* your experiment in one respect — it gives you a prompt-token-heavy workload alongside GSM8K's output-token-heavy one, which is directly useful for testing Cho et al.'s input/output asymmetry across two different token mixes rather than one.

**Considered and rejected:**

- **MATH-500** — below the floor for 1.7B.
- **MMLU / ARC** — easy grading and fine debate behaviour, but decomposition into independent subtasks is artificial, so planner-worker becomes a costume.
- **StrategyQA** — decomposes well and grades cleanly (binary), but binary answers make debate degenerate: with two agents and two options, "reconsidering" is a coin flip and majority vote is undefined. Avoid binary-answer datasets for debate specifically.

**If you take only one:** GSM8K, and state plainly in the limitations that planner-worker decomposes into sequential steps rather than independent subtasks on this dataset. That's an honest, survivable caveat — but it does weaken the third topology, which is why I'd pay the extra block for HotpotQA.

**Also:** fix the answer-extraction rule identically across all topologies, cap `max_tokens` identically across all roles (512 is reasonable) so no role can run away, log truncations, and **log accuracy** — not because the paper is about accuracy, but because (a) a reviewer will ask whether the extra energy bought anything, and (b) a topology that collapses at temp 1.0 means you're measuring degeneration.

---

## 6. Serving stack

**Recommendation: llama.cpp server, CUDA build, on *both* platforms.**

| Option | Verdict |
|---|---|
| **Ollama** | **No.** Wraps llama.cpp but owns model lifecycle — it auto-unloads after an idle timeout, which will silently reload your model mid-run and contaminate a block. Also opaque sampler defaults and less control over caching/slots. |
| **vLLM on Jetson** | **No.** Its entire value is continuous batching and paged attention, both of which you are disabling. You get the heavier, more variable allocator and none of the benefit. Also a rougher build target on aarch64. |
| **TensorRT-LLM** | **No.** Fastest, but build fragility on Jetson, per-config engine builds inside your loop, and you cannot realistically match it on a shared A100 without root. |
| **llama.cpp server** | **Yes.** Single process, explicit `--n-gpu-layers`, per-request `cache_prompt` control, returns exact `prompt_n` / `predicted_n` and server-side timings, deterministic given a seed. |

The decisive argument is cross-platform: **llama.cpp is the one stack you can run identically on Orin NX and on a shared A100 without root.** That means your NX-vs-A100 comparison isn't confounded by serving stack. Yes, llama.cpp is not how anyone would normally serve an A100 — say so in the limitations. Comparability is worth more here than A100 throughput.

**Flags to pin and never change across the 12 configs:**

- `--n-gpu-layers 999` (everything on GPU)
- `--ctx-size` — **one fixed value for all configs**, sized to the worst-case debate context. If ctx varies per config, KV allocation varies, memory footprint varies, and your power floor varies with it.
- `--parallel 1`, continuous batching off, `--no-context-shift`
- `--no-mmap` — weights in a fixed allocation rather than page-cache-dependent, avoiding first-call page faults
- Same `--flash-attn` setting everywhere
- Explicit per-request seed
- Warm-up calls after every server start, discarded

---

## 7. Orchestrator — write it yourself

**Do not use AutoGen, LangGraph, or CrewAI.** Concretely, not on general principle:

- They own the async event loop and will issue concurrent calls — breaks §2.1.
- They inject their own system prompts, message serialisation, and scaffolding, so your `prompt_n` includes framework padding you can't fully account for.
- They perform internal exception-driven retries silently, so your retry count — a key variable — is wrong.
- The GPIO trigger must be the *innermost* wrapper around the HTTP send/receive. A framework makes that placement hard to guarantee and harder to defend.
- Version drift across a six-week run.

What you actually need is a few hundred lines:

```
call()          # THE single choke point. Global lock → GPIO high → POST → 
                # response → GPIO low → release → log. Nothing else in between.
baseline()      # 1 call
debate()        # fixed rounds, symmetric roles, vote or synthesis
solver_critic() # loop until accept or iteration cap
planner_worker()# plan → dispatch subtasks serially → synthesise
driver()        # 12 configs × items × seeds, randomised order
logger()        # append-only, one row per call, flush immediately
```

The entire argument to a reviewer is: *between trigger-high and trigger-low, nothing happens except one model call.* Keep that literally true.

### Per-call log schema (this is your unit of analysis)

```
run_id, item_id, dataset, topology, temperature, seed, repetition,
role, round_index, call_index_in_task, is_retry, retry_reason,
prompt_n, predicted_n, cached_n, finish_reason, truncated,
wall_clock_ms_orchestrator, server_prefill_ms, server_decode_ms,
trigger_high_ts, trigger_low_ts,
energy_J_external, energy_J_ina_vdd_in, energy_J_ina_cpu_gpu_cv, energy_J_ina_soc,
idle_W_reference, temp_C_soc_before, temp_C_soc_after, temp_C_cpu, temp_C_gpu,
freq_gpu, freq_cpu, freq_emc, nvpmodel_mode, fan_pwm,
parse_ok, answer_extracted, correct
```

**Input and output tokens are logged separately and never summed.** Cho et al.'s output-asymmetry finding is one of your three research questions and you cannot test it from a total. This is the single most important line in the schema.

---

## 8. Device-state control on the NX

This is where edge energy experiments usually die quietly.

- **`nvpmodel -m <fixed>`.** Orin NX **8GB offers 10W / 15W / 20W / MAXN — there is no 25W mode** (that's the 16GB variant). Fix at **20W** and verify it's the Pareto point on your unit; 25W was the reported sweet spot on Orin-class hardware and 20W is your nearest analogue. **Never MAXN**, it's variable by design and defeats the whole point of fixing a power budget.
- **`jetson_clocks`** to lock clocks. Without it, DVFS makes identical work cost different energy depending on governor state, and you are measuring the governor. Log the achieved frequencies anyway to confirm the lock held.
- **Fan at fixed manual PWM (100%), not auto.** This is the one people miss. The fan sits *inside* your whole-device measurement boundary. An auto fan ramps with temperature, so a hot call literally costs more measured watts because of the fan, not the compute — and that correlates with workload, which is exactly how a confound becomes a finding. Fixed PWM makes it a constant that idle subtraction removes.
- **Cooldown gate before every call** — see §8.1, this is the single most important control in the whole design.
- **Strip the device**: see §8.2 below — this is more involved than it sounds on stock Ubuntu.

### 8.1 Cooldown — why, and how much

**Yes, there is a cooldown before every call.** Not for appearances — it removes a confound that would otherwise be perfectly correlated with your independent variable.

**The physics, plainly:** silicon leaks current even when it isn't switching, and that leakage rises steeply with temperature — roughly doubling every ~10 °C. So a hot chip burns more watts than a cold one *doing exactly the same work*. Your briefing already acknowledges this (§5.6: "a device running hot draws more power for the same work").

**On magnitude — and why magnitude is the wrong thing to focus on.** At the temperatures you'll see, this is probably a low-single-digit percentage effect on whole-board power. That sounds ignorable. It isn't, and the reason is direction rather than size:

> The thermal effect pushes in **exactly the same direction as your hypothesis.** Debate issues more calls, so debate runs hotter, so debate measures as more energy-hungry per call — which is precisely the finding you would be claiming as evidence that coordination costs energy.

A confound that adds noise is annoying. A confound that biases *toward the result you expect* is disqualifying, because you cannot tell it apart from the effect. That is why this control matters even at a few percent, and why it's worth unlimited wall-clock time to remove.

**Why it's fatal here specifically.** Without a gate, a call's thermal state is a function of *what ran immediately before it* — and that is determined by topology. A debate task issues 5 back-to-back calls; the baseline issues 1. So the 5th debate call runs on hotter silicon than any baseline call ever does, systematically, every item.

```
topology  →  preceding call density  →  junction temperature  →  measured energy
```

That is a confound running straight through your independent variable to your dependent variable. Uncontrolled, debate would appear more energy-hungry per call partly because of accumulated heat, not because of coordination — and you would have no way to separate the two after the fact. Since the whole paper is "multi-agent coordination costs energy that proxies don't capture," inflating exactly that quantity with a thermal artifact is the worst available failure.

**Gate to a band, not a ceiling.** Naively you'd wait until temp drops below a threshold. Better to hold a *band* (e.g. 50 ± 2 °C) and gate in both directions:

- Too hot → wait.
- Too cold (start of a block, after a pause) → run discarded warm-up calls until in band.

A cold device is as unrepresentative as a hot one, and blocks would otherwise start with systematically cheap calls. Gating both directions makes every measured call start from the same thermal state regardless of position in the run.

**Add a timeout** on the gate so a warm ambient day can't hang the run, and **log every timeout** — a call that started out of band is a flagged data point, treated like the A100's contaminated-GPU exclusions in §5.4 of your brief.

### 8.1.1 Why you should not believe my cooldown estimate — and why that's fine

**The estimate is not load-bearing.** This is the important structural point, and it's worth being explicit about because "10–20s" looks like a number I know and it isn't.

There are two ways to build a cooldown, and they fail very differently:

| | **Open loop** (fixed wait) | **Closed loop** (temperature-gated) |
|---|---|---|
| Rule | "wait 20 seconds" | "wait until SoC temp is in band" |
| Depends on my estimate? | **Yes, entirely** | **No** |
| If ambient rises 5 °C | Silently wrong — 20s no longer returns you to the same temperature, and every call starts hotter | Self-correcting — waits longer, same end state |
| If the estimate is 3× off | Confound survives, undetected | Just slower |
| What it guarantees | The same *wait* | The same *state* |

**Use the closed loop.** Then the only thing my estimate affects is how long the campaign takes — and you have just told me that doesn't matter. It has no bearing on whether the measurement is correct. A wrong estimate costs you time; a wrong *gate* costs you the result.

That is the answer to "why should I believe an estimate": you shouldn't, and you don't have to. Build the gate so that being wrong is cheap.

**Where my 10–20s actually came from,** so you can weigh it: published benchmarks on an Orin Nano Super reported sub-1B models averaging ~57 °C, larger models ~61 °C, and peak junction ≤73 °C with no throttling observed. From that I inferred that a 1.7B model with the fan pinned at 100% will not get very hot, so the return to band should be quick. That chain has three weak links — different board, different chassis and airflow, different room, different fan setting, different model. It is an order-of-magnitude sanity check, not a measurement. Treat it as "probably seconds, not minutes" and nothing more.

### 8.1.2 Measuring the real thing (about 30 minutes)

1. Fix the power mode, run `jetson_clocks`, pin the fan at 100%, boot to `multi-user.target`. Identical to run conditions.
2. Let the device sit until SoC temperature is flat. Record it as `T_idle`, and record room temperature.
3. Run a sustained generation for ~60 s while logging SoC temperature at 1 Hz. Record the plateau as `T_peak`.
4. Stop the load. **Keep logging at 1 Hz until the temperature is flat again.**
5. Plot it. The decay is close to exponential:

```
T(t)  ≈  T_idle + (T_peak − T_idle) · e^(−t/τ)
```

&nbsp;&nbsp;&nbsp;&nbsp;`τ` is the thermal time constant. Time to come within `δ` of idle ≈ `τ · ln((T_peak − T_idle) / δ)`.

6. **Repeat five times.** Consistency across repeats is what makes it a measurement rather than an anecdote. Report the spread.
7. Repeat once more in the afternoon, or whenever the room is warmest, to see how much ambient moves it.

You now have a number measured on your unit, in your room, with your fan setting, with a stated repeatability — which is exactly the standard the rest of your paper is held to. Report it in the methods alongside the band you chose.

**Choosing the band, given step 7:** set the target *above* the warmest idle temperature the room will produce. If the lab reaches 32 °C in the afternoon and idle SoC sits at ~48 °C then, do not set a 45 °C target — the gate would hang every afternoon. Pick a band the device can always reach (say 52 ± 1 °C), and add a timeout that flags rather than hangs.

**Also gate the block boundaries**, not just calls: a fixed longer settle (several minutes) after any server start or nvpmodel change, before the first measured call.

### 8.2 OS baseline and Ubuntu's background timers

The Jetson runs Ubuntu because JetPack *is* Ubuntu — L4T is an Ubuntu-derived rootfs with NVIDIA's kernel and driver stack on top. There is no supported alternative distro for Orin NX, so this is the expected configuration.

**Version mapping (both support Orin NX):**

| JetPack | Jetson Linux / L4T | Ubuntu | CUDA | Kernel |
|---|---|---|---|---|
| 6.2.x | r36.4.x | 22.04 | 12.6 | 5.15 |
| 7.2 | 39.2 | 24.04 | 13.2.1 | 6.8 |

**Pin one and never upgrade mid-project.** A JetPack change during the run alters the CUDA version, the memory allocator, the nvpmodel mode definitions, and potentially the INA3221 sysfs paths your measurement code reads. Any of those invalidates cross-block comparability. Decide now, freeze, and record the exact version in the methods:

```
cat /etc/nv_tegra_release
dpkg-query --show nvidia-l4t-core
apt-cache show nvidia-jetpack | head
```

If you're already on ≥ 6.2.2 and it works, staying is the lower-risk option — 6.2.x is far more field-tested for aarch64 llama.cpp builds. Jump to 7.2 only if you're below the IOVA threshold anyway and are going to reflash regardless.

**Never `do-release-upgrade` a Jetson.** Ubuntu on Jetson is not stock Ubuntu; a distro upgrade will break the NVIDIA driver stack. Reflash with the JetPack image instead.

#### Ubuntu's default timers will fire inside your run

This is the part people miss, and a 5–6 day continuous measurement is long enough that essentially all of these *will* land inside a measured block:

| Service | What it does mid-run |
|---|---|
| `apt-daily.timer`, `apt-daily-upgrade.timer` | unattended-upgrades wakes up, hits network, CPU, and NVMe |
| `snapd.refresh.timer` | snap refresh — notoriously unpredictable CPU spikes |
| `man-db.timer` | daily index rebuild |
| `fstrim.timer` | weekly NVMe TRIM — guaranteed to land inside a 6-day window |
| `motd-news.timer` | network fetch on login |
| `tracker-miner-fs` / GNOME indexers | only if a desktop session is up |

**Do:**

- Boot to `multi-user.target` — no GNOME. On JetPack 6+ the desktop is Wayland by default and the session costs real, *variable* power inside your whole-device measurement boundary.
- Mask the timers above, then verify nothing is scheduled inside a block: `systemctl list-timers --all`
- Disable WiFi/BT radios; keep wired ethernet for SSH only.
- **Disable zram swap** (`nvzramconfig.service`). Jetson enables compressed swap by default. If it ever activates, you are measuring zram's CPU-side compression, not inference — and on 8GB with a ~3.4 GB footprint it shouldn't trigger, which is exactly why a silent activation would be so easy to miss. Disable it, and log `/proc/vmstat` swap counters anyway as a tripwire.
- Confirm the cpufreq governor stays where `jetson_clocks` put it — Ubuntu's `ondemand` service can re-assert itself across reboots.
- No USB peripherals, nothing on cron, no other users logged in.
- **Randomise config order within each block.** If you run all of temp 0.2, then all of 0.7, then all of 1.0, thermal and any slow drift becomes perfectly confounded with temperature. Randomise, or at minimum counterbalance.
- **Software path sampling**: the INA3221 has a configurable conversion time; with typical settings the full 3-channel cycle is ~6.6 ms (~150 Hz), and `tegrastats --interval` controls *reporting*, not hardware sampling. Read the sysfs hwmon nodes directly rather than parsing `tegrastats`, and check `update_interval` to know your true rate. This is roughly an order of magnitude below your external rig's 1–1.5 kHz — which is precisely the comparison you want to make.

---

## 9. Defining the monetary-cost proxy on hardware you own

There is no dollar figure on a Jetson, so cost must be **imputed**:

```
cost = prompt_n × price_in + predicted_n × price_out
```

using a published per-token schedule for a comparable hosted model, stated explicitly in the methods.

**The analytically important point, which is worth building the paper around:** imputed cost is a *linear function of the two token counts by construction*. It is not an independent third proxy — it is a **weighted token count**. And that is where the interesting result is hiding: commercial pricing weights output tokens roughly 3–5× input, and Cho et al. found output tokens carry disproportionate energy. So imputed cost may well track measured energy **better** than raw total tokens do.

If that is what you find, it is a clean, specific, publishable result — and it answers the review's fourth future direction directly, rather than reporting cost as a mysterious third quantity that happened to correlate.

Also report **actual electricity cost** (measured Joules × local tariff) as the reference for what "cost" should mean if it were measured rather than proxied.

---

## 10. Analysis plan — sketched now so the logging is right the first time

- **Unit:** the call. Mixed-effects model, `energy_J ~ proxy + (1|item)`, fitted per topology and pooled.
- **Compare marginal R² across:** `total_tokens`; `prompt_n + predicted_n` as *separate* terms; `wall_clock`; `imputed_cost`.
- **Output-asymmetry test (RQ1):** is the fitted coefficient ratio `predicted_n : prompt_n` consistent with Cho et al.'s implication, and does it hold across all three structurally distinct topologies?
- **Temperature-as-cause (RQ2):** mediation — temperature → retry_count → energy. Requires retry_count as a logged variable and requires retries to actually happen (hence §2.4). Report the *direct* effect (energy per call at higher temperature) separately from the *indirect* effect (more calls per task).
- **Task-level repeat:** sum energy over calls per task and refit. Call-level and task-level answers may differ — and that difference is itself a finding, because the agent-efficiency literature reports at task level.
- **NX software-vs-hardware path:** regress INA-derived energy on external-rig energy per call. The slope and R² tell you exactly how much to trust "we measured energy with the onboard counters," which every other paper in this space relies on. This is a standalone contribution and arguably the most immediately citable thing you'll produce.

---

## 11. What to nail down before writing any code

0. **Check JetPack version and freeze it.** See the warning at the top and §8.2. Flash before anything else — it changes allocator behaviour, so every measurement below must be taken on the final image.
1. **Measure the thermal time constant** (§8.1.2 — a ~30 minute procedure, repeated 5×). Not to schedule around, since the timeline is free, but to choose the gate band and confirm the gate actually converges under the warmest room conditions you'll see.
2. **Measure decode and prefill tok/s at BF16/FP16.** Prefill matters more on the 8GB variant (lower GPU clock). Confirm or replace the §4.1 assumptions.
3. **Pilot for the go/no-go checks** (§3.3, §5.1): 20 items through debate → what fraction of agents change their answer between rounds? And what is baseline single-call accuracy — is it in the 45–70% band? Both must pass before the full run.
4. Model + pinned revision hash. F16/BF16 GGUF, per §3.2.
5. **Measure the longest debate and HotpotQA context in tokens, then set `--ctx-size` once for all configs.** On 8GB this is a memory decision, not just a config decision. Size to worst case with margin, then freeze.
6. **Characterise the trigger line**: measure the latency between the GPIO write and the HTTP send (and the reverse on return). Quantify it, subtract it, and report the residual uncertainty. A reviewer will ask what your attribution error bar is.
7. **Pilot: 10 items × 12 configs.** Look at the variance, then size the real run from measured numbers rather than the estimates above.

---

## Open question

**What do the agents reason about?** This is the last undecided input, and it's load-bearing. The dataset has to satisfy all three topologies simultaneously:

| Requirement | Imposed by | Fails when |
|---|---|---|
| A defensible answer a peer can argue you out of | Debate | Tasks with no single right answer — agents converge by politeness, not reasoning |
| A specific, nameable error for the critic to catch | Solver-critic | Open-ended generation — the critic emits vague "could be clearer" feedback and the loop never terminates on merit |
| Genuinely **independent** subtasks | Planner-worker | Sequential reasoning chains — workers can't run without each other's output, so the topology is a costume |
| Programmatic answer extraction and grading | All | Free-text answers — you lose the accuracy covariate that tells you whether the extra energy bought anything |

Planner-worker is the binding constraint and the reason a single math dataset isn't ideal. Candidates that satisfy all four:

- **GSM8K / MATH-500** — canonical debate benchmark (Du et al. 2024), trivially gradeable, strong critic signal. Weak on independent decomposition.
- **HotpotQA / 2WikiMultiHopQA** — multi-hop questions decompose into genuinely independent lookups. Strong on all four, and the closest thing to "natural-language reasoning rather than code or tool calls" that your review's RQ1 asks for.
- **MMLU subsets** — easy grading, good debate behaviour, but decomposition is artificial.
- **StrategyQA** — implicit multi-step reasoning, decomposes reasonably, binary answers grade cleanly.

If a domain has been suggested to you (energy, sustainability, an application area tied to the lab), say so — a domain-specific QA set can work provided it clears all four rows above, and domain relevance is worth something in the writeup.
