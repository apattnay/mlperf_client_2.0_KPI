# KPI-hub Integration Notes & Engineering Log

Durable engineering notes for the `tools/KPI-hub` integration, the MLPerf Client v1.5→v2.0
migration, and the KPI instrumentation built on top of it in this repo. This document lives in
git (not in any external/AI-assistant-specific memory store) so it survives regardless of what
tooling is used to work on this repo in the future.

## 1. Vendoring model

- `tools/KPI-hub` is a **vendored snapshot** (plain files), **not** a git submodule — so
  contributors cloning this repo without access to `intel-sandbox/KPI-hub` still get everything
  needed to build and run. Provenance is recorded in `tools/KPI-hub/.vendor-snapshot.json`
  (source repo, commit SHA, date, and any outstanding local patches).
- To pull a newer KPI-hub snapshot (requires access to the source repo): run
  `tools\sync_kpi_hub.ps1`, review `git diff tools/KPI-hub`, then commit.
- **GOTCHA**: `sync_kpi_hub.ps1` does `Get-ChildItem $dest -Force | Remove-Item -Recurse -Force`
  then copies in the fresh clone — this **wipes the entire `tools/KPI-hub` directory**, including
  any `tools/KPI-hub/upstream/*.patch` files and doc-only local edits not yet upstreamed (e.g. a
  `DATA_SOURCES.md` section that was never part of an upstreamed commit). Always regenerate any
  needed patch files from git history **after** running the sync (not before — the sync deletes
  them), reapply, restore any wiped doc-only sections via `git checkout -- <file>`, and update
  `.vendor-snapshot.json`'s `local_patches`/`vendored_commit`/`vendored_at` fields.
- **GOTCHA**: `py_compile` run against a file inside a temp clone creates a `__pycache__/*.pyc`
  that `git add -A` will happily stage — `git reset` it back out before committing.

## 2. Upstreaming process (this repo owns `intel-sandbox/KPI-hub`)

Since the repo owner (`apattnay`) also owns `intel-sandbox/KPI-hub`, local patches are merged
directly (fast-forward, no PR needed) rather than opened as pull requests:

1. Clone KPI-hub fresh into a temp dir, create a feature branch.
2. Generate a clean patch for just the relevant file(s):
   `git diff <commit>^ <commit> --output="<path>.patch" -- tools/KPI-hub/<file>`
   — **use `--output=`, not a shell pipe** (`| Out-File` / `>`). Piping a `git diff` through
   PowerShell corrupts unicode characters (`→`, `─`, `—`, `▲`, `●`) in unrelated context lines
   elsewhere in the diff, which then makes `git apply` fail with a confusing
   `"patch does not apply"` error at a seemingly-unrelated line. `--output=` writes the file
   directly via git, bypassing shell pipe encoding entirely.
3. Apply patches in the KPI-hub clone with `git apply -p3 <patch>` (the `-p3` strips the
   `a/tools/KPI-hub/` prefix baked into patches diffed from inside this vendoring repo, since they
   need to apply at the KPI-hub repo root instead).
4. Set the correct git identity in the clone (`git config user.name/user.email` —
   `Aurodeepta Pattnayak <aurodeepta.pattnayak@intel.com>`), commit via `git commit -F <msgfile>`
   (a message **file**, not `-m "..."` with embedded literal double quotes — that broke PowerShell
   parsing once and produced a bogus, unrelated error).
5. Check `git rev-parse origin/main` still matches the clone's base commit (fast-forward safe),
   then `git push origin <branch>:main`, then delete the feature branch.
6. Back in this repo: run `tools/sync_kpi_hub.ps1` to pull the newly-merged commit, restore any
   wiped doc-only sections, and trim `.vendor-snapshot.json`'s `local_patches` to only what's
   still genuinely not upstreamed.

As of 2026-09-23, **all** local patches from this project (title rename, device_type-aware
efficiency, KV-cache estimate panel + its `DATA_SOURCES.md` docs, tool-call+CPU-timeline,
cold/warm+Prefill/ITL columns, Roofline projection) are upstreamed to `intel-sandbox/KPI-hub`
main (commit `f94664c`) — `.vendor-snapshot.json`'s `local_patches` is an empty array, a clean 1:1
mirror of upstream.

## 3. Git / GitHub gotchas hit in this repo

- **Stale git identity**: this machine's git config (both `--global` and repo-local) had a stale
  identity from a previous user. Always verify `git config user.name`/`user.email` early on a
  new/shared machine — GitHub attributes commits by author *email*, not username or permissions,
  so a wrong local config silently misattributes every commit. Fix already-pushed bad-author
  commits with `git filter-branch -f --env-filter "GIT_AUTHOR_NAME=...; ..."`, not
  `git rebase --exec "git commit --amend --author=..."` (the `<>` in an author string plus nested
  quoting breaks on Windows PowerShell, and piping through an external `.cmd`/`.sh` helper also
  fails since git's `--exec` runs via git-bash's `sh`, which mangles Windows backslash paths).
- **Git LFS rejected on public forks**: GitHub blocks *new* LFS object uploads to public forks
  ("can not upload new objects to public fork ..."), even though `git lfs track`/commit/push all
  appear to succeed locally — the push fails at the batch-upload step. Plain git blobs are the
  simpler, working alternative for moderate-sized binary-ish artifacts (tens of MB).
- **Stale LFS pointer re-staging**: if you `git lfs untrack` + re-`git add` a path that was
  already committed via LFS in a prior (even if since-reset) commit, `git add -A` alone may
  re-stage it as a stale ~132-byte LFS pointer blob instead of real content. Force a clean
  re-stage with `git rm -r --cached <path>` then `git add <path>`; verify via
  `git cat-file -s :<path>` before committing.

## 4. MLPerf Client v1.5 → v2.0 migration

- Real v1.5-installed `mlperf-windows.exe` does **not** support the `IsAgentic` scenario field at
  all (schema validation rejects it) — that's a v2.0-only feature. All presets (1-6) now
  standardize on the official v2.0.0 Windows x64 CLI release
  (`mlcommons/mlperf_client` releases tag `v2.0`, asset
  `mlperf-client-2.0.0-c8d2dc0-windows-x64.zip`), installed to
  `C:\Applications\mlperf_client\mlperf_v2p0\` via `tools/setup_mlperf_v2.ps1` (idempotent).
- Presets 1/2 point at the config bundled inside the v2.0 install itself
  (`llm/Llama3.1/Intel_NativeOpenVINO_{NPU,GPU}_Default.json`, resolved relative to
  `--mlperf-dir`). Presets 3/4's custom repo configs were rewritten from v1.5-style relative
  `file://./dependencies/...` paths + the v1.5-only `IHV_NativeOpenVINO.dll` shim to plain HTTPS
  URLs matching the v2.0 CDN scheme. All presets default to `download_behaviour: "normal"`.
- Presets 5/6 (`tools/run_kpi_preset.py`) are the **real** "SWE Agent" agentic scenario
  (`data/configs/vendors_default/agentic/Llama3.1/Intel_NativeOpenVINO_{NPU,GPU}.json`), distinct
  from presets 3/4 (a simpler code-analysis, non-agentic stand-in). Presets 5/6 need network
  access (`-b normal`, nothing pre-cached) plus `--python-path system` (no bundled Python in this
  mlperf build).

## 5. Corporate proxy (Intel network)

`client.mlcommons-storage.org` (mlperf's download CDN) is not reachable directly on this network —
it requires `http://proxy-dmz.intel.com:911/`. `mlperf-windows.exe` needs **both**:
- `HTTP_PROXY`/`HTTPS_PROXY` env vars in the shell, **and**
- machine-wide WinHTTP proxy (`netsh winhttp set proxy proxy-dmz.intel.com:911 "<bypass list>"`)

simultaneously — env vars alone are not enough, and the WinHTTP setting (which *does* persist
machine-wide across sessions) alone is also not enough. Run `. .\tools\set_proxy_env.ps1` in
**every new terminal session** before running presets 5/6 (it sets both, but the env-var half is
session-scoped). Manually staging v1.5 model/IHV files into v2.0's expected cache paths (or
writing a fake `url_cache.json`) does **not** work reliably — v2.0 checks downloads against an
internal per-version cache/hash record and flags manually-placed files as "exists but not match
the uri in the cache", then still treats them as missing at the prepare stage. Fix the proxy
instead of trying to fake the cache.

## 6. `run_kpi_workflow.py` — parser and instrumentation notes

Orchestrates: starts `sample_utilization_fast.py` (HW telemetry sampler), runs
`mlperf-windows.exe`, parses per-inference stages from `Logs/<scenario>_executor.log` into
`workflow_kpi.json` + `experiment.json`, then calls `plot_utilization_interactive.py`
(`dashboard.html`) and `generate_kpi_report.py` (`kpi_report.html`). Output lands under
`kpi_runs/<name>_<timestamp>/` — **gitignored by default** (see §8); only commit a specific run
directory when explicitly asked.

- **No `Category:` line in agentic logs**: agentic executor logs never emit a `Category:` line
  (that's v1.5-style only) — stages were all landing as `NN_unknown`. Fixed by capturing the
  (multi-line, unbracketed) `"User prompt:"` body and classifying it via `_classify_prompt()` /
  `_PROMPT_CATEGORY_HINTS` keyword matching (`"Warmup."` → warmup, `"SWE-Agent"` → swe_agent,
  `"DATA ANALYST"` → data_agent). Later turns in a multi-turn conversation often don't repeat the
  system-prompt keyword, so category is only overwritten on a successful match, carrying forward
  the last known category rather than resetting to unknown.
- **Pipelined logging with `Delay=0`**: with back-to-back prompts, the executor log pipelines
  logging — stage N's `power_end`/`"Ran inference"`/`TTFT` print, then stage N+1's `power_begin`
  fires, and *only then* does stage N's trailing `"Input tokens:"`/`"Tokens Per Second:"` block
  print. A naive single-`pending`-dict parser gets clobbered by the next `power_begin` before
  stage N can be emitted. Fixed by tracking two slots (`open_stage` being built, `closing_stage`
  waiting on its trailing stats), and snapshotting `category` **into** each stage dict at
  `power_begin` time (a shared/global category var would otherwise get overwritten by the next
  prompt's line before the pipelined stage is emitted, mislabeling it).
- **RAPL power was silently disabled**: `sample_utilization_fast.py` never received `--power`, and
  the default `"lightweight"` HW profile's `"power": "pdh"` setting does **not** auto-enable RAPL
  (only `"emon"`/`"both"` do — a naming trap, since "pdh" sounds like it means "read via
  PDH/RAPL"). Fixed by unconditionally passing `--power` to the sampler.
- **Stage naming didn't match prompt files**: agentic stage names were just `NN_swe_agent`
  repeated 3-4x per round with no way to tell which turn was which, not lining up with the
  underlying `data/prompts/llama_3_1_8b_instruct/{swe_agent,data_agent}/*_N.md` files (0-indexed
  per round). Fixed with a per-round `agent_turn_counters` dict, incremented on each
  `"*_agent"`-suffixed category and cleared on every `"warmup"` stage — stage names are now
  `NN_swe_agent_0`, `NN_swe_agent_1`, `NN_swe_agent_2` per round, matching the `.md` suffix
  exactly. Validated end-to-end against fresh preset 5/6 runs.
- **Model weight sizing silently broken for `https://` models**: `resolve_model()` only sized
  `model_weight_mb` for `file://`-style configs; every LLM/agentic preset here uses
  `https://`-downloaded models, so it silently stayed `0`. This cascaded into `Est. Parameters`
  falling back to a hardcoded `~4.0B` (half the real ~8B) and `DRAM BW Achieved`/
  `Memory BW Utilization` always showing `0.0`/`0.0%` in the Efficiency Analysis table. Fixed by
  checking the predictable local cache path `mlperf-windows.exe` actually downloads `https://`
  models into: `dependencies/llm/<scenario>/models/<backend>/<model_name>/` (confirmed on disk:
  CHw=3835MB, GRw=3950MB, both correctly resolve to ~8.0B params once sized).
- **Tool-call identification**: `mlperf-windows.exe` writes `Logs/results.json` (JSON-lines, one
  entry per run) with an `"Output"` array — the model's raw generated text per non-warmup turn.
  The agent system prompts define an Anthropic-style `tool_use` JSON format
  (`{"type": "tool_use", "name": "<tool>", "input": {...}}`); `extract_tool_calls()` regex-matches
  these per `Output` entry and merges counts onto the corresponding stage (ordinal alignment:
  `Output[i]` maps to the i-th non-warmup stage in dict order). **Caveat**: this reflects what the
  model *attempted* to call, not proof the harness executed it end-to-end — real code execution
  doesn't always correlate 1:1 with an `execute_command` call appearing in `Output`.
- **Cold/warm + Prefill/ITL**: `llama3_executor.log` lines we previously discarded turned out to
  contain everything needed to reconstruct inference phase boundaries:
  - `"- inference task added. history: N, user: M, expected: K"` (logged upfront, same order
    stages are later emitted) → per-stage `is_cold` (`history == 0`), `history_tokens`,
    `turn_new_tokens`. `history` is literally the *previous turn's own generated-token count*, fed
    back in as context.
  - `"TTFT Xms, 2nd+ token latency Yms"` + `"Average 2nd+ Token Latency: (ms) Y (+-stddev)"` →
    `avg_itl_ms`/`itl_stddev_ms` (decode-phase inter-token latency, was previously discarded
    entirely, leaving the report's Avg ITL column always `-`), with
    `prefill_ms_est = ttft_ms - avg_itl_ms` (treats the first token as costing one normal decode
    step; the remainder of TTFT is attributed to prefill/KV-cache build).
  - True p50/p99 ITL remains **not derivable** — the log only exposes mean + std-dev per turn, not
    a full per-token latency distribution; would need further instrumentation upstream in
    `mlperf-windows.exe` itself.
  - Full methodology + a TTFT-scaling proof that the OpenVINO GenAI pipeline reuses/extends the KV
    cache across turns (rather than re-prefilling from scratch) is in
    `docs/AGENTIC_WORKFLOW_CHARACTERIZATION.md` §8.

## 7. Roofline projection

Added a full Roofline-model analysis (arithmetic intensity vs. achieved GFLOPs/s for prefill vs.
decode phases, plotted against device memory-bandwidth/compute roofs) to `kpi_report.html`. Full
methodology, formulas, and a worked numeric example are in
`docs/AGENTIC_WORKFLOW_CHARACTERIZATION.md` §9. Key implementation notes:

- Prefill AI scales with prompt length (`2 × N_input_tokens / bytes_per_weight`); decode AI is a
  small constant (`2 / bytes_per_weight` — `4 FLOPs/Byte` for int4) regardless of context length.
- For NPU, no vendor compute spec is derivable from this benchmark's HW probing (only a generic
  `"Intel(R) NPU"` string, no EU count/clock) — the compute roof is instead derived **empirically**
  from the run's own best prefill-phase throughput, clearly labeled as such rather than presented
  as a vendor spec.
- **RESOLVED 2026-09-23**: a fresh NPU-vs-iGPU comparison (see
  `docs/AGENTIC_WORKFLOW_CHARACTERIZATION.md` §10) showed NPU winning prefill by 2-4x and iGPU
  winning decode by ~1.5x — and surfaced two data-quality issues, both investigated with hard
  evidence (no speculation):
  1. iGPU's `Memory BW Utilization` came out to **108.6%** (physically impossible — achieved can't
     exceed true peak). Root cause: the hardcoded `89 GB/s` constant was measured on a *different*
     reference platform — `DATA_SOURCES.md`'s own "Hardware Platform Reference" table documents it
     as 2 channels × 64-bit DDR5-6400 (102.4 GB/s theoretical). Querying this machine's actual
     memory directly (`Get-CimInstance Win32_PhysicalMemory`) found 8 channels × 16-bit ×
     8533 MT/s configured clock = **136.5 GB/s** real theoretical peak, a fundamentally different
     (higher-bandwidth) memory subsystem. **Fixed**: `_collect_sut_info()` now detects real DRAM
     bandwidth per-machine via PowerShell/CIM (not `wmic` — confirmed broken/deprecated on this
     exact machine, returning `"ERROR: Invalid namespace"`), and `_build_efficiency_html`/
     `_build_roofline_html` use the detected value, falling back to the old constant (now labeled
     `"(DDR5 assumption)"`) only if detection fails. Recomputed utilization with the correct peak:
     NPU 47.8%, iGPU 70.8% — both physically valid, ratio matches the ~1.5x decode-speed
     difference already established.
  2. The pre-existing `IGPU_MEASURED_TOPS_INT4 = 0.078` TOPS (78 GFLOPs/s) constant isn't simply
     stale — `DATA_SOURCES.md` §6/§10 documents it was measured via a synthetic SYCL GEMM
     microbenchmark on only 16 of a **128-EU reference iGPU**, a different chip than this
     machine's 96-EU iGPU, using a fundamentally different method (isolated matmul kernel vs. real
     decode throughput). Left as a reference data point but relabeled `"(different reference
     chip)"` in the report so it isn't read as directly comparable.

## 8. `kpi_runs/` data policy

`kpi_runs/` is **gitignored by default** (as of 2026-09-18, reversed from an earlier session-wide
"commit everything" decision). After running a preset, leave the output local/untracked — only
`git add -f` and commit a specific run directory when explicitly asked to "push it" / "commit this
run". A handful of curated reference runs (the original preset 1-6 sweep, an ITL-instrumentation
validation run, and an NPU-vs-iGPU roofline comparison) are already committed and indexed in
`docs/AGENTIC_WORKFLOW_CHARACTERIZATION.md` §7.

## 9. Local dev environment

- Python env: repo-root `.venv` (gitignored via `.venv*/`), created via
  `.\tools\setup_kpi_hub_env.ps1`. Deps from `tools/KPI-hub/requirements.txt` (psutil, pywin32,
  pandas, plotly).
- **GOTCHA**: a stray `PYTHONHOME` env var (e.g. leaked from another tool/session) crashes
  `.venv\Scripts\python.exe` immediately with `"Fatal Python error: Failed to import encodings
  module"`. Always `Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue` before invoking the
  venv Python in a fresh terminal.
- Fresh-machine setup: clone repo → `.\tools\setup_kpi_hub_env.ps1` → `.\tools\setup_mlperf_v2.ps1`
  → `. .\tools\set_proxy_env.ps1` (Intel network only, re-run per terminal) →
  `.venv\Scripts\python.exe tools\run_kpi_preset.py --preset N`. Full details in
  `docs/run_benchmark_prompt.md`.
- A GPU (iGPU) driver crash (`CL_OUT_OF_RESOURCES`) was seen once on v1.5; not reproduced since
  the v2.0 migration. Keep an eye out if it recurs.
