# Renode TIA: Testwise Coverage for Zephyr SIL Tests — Decisions

Outcome of a design review on 2026-09-24.

## Goal
Convince Zephyr users doing Software-in-the-Loop testing (ideally with Renode) to use Teamscale TIA for faster
test feedback. Coverage is of the **guest firmware** (not Renode's own C# code).

## Decisions
1. **Scope:** guest firmware coverage (executed PCs → DWARF → source lines).
2. **Renode-side global switch (C#)**, modelled on `EnableProfilerGlobally`: auto-attaches to every CPU as it is
   created (machines don't exist at `Test Setup`; tests create them in the body).
3. **Record executed address ranges per test** (translation blocks / unique PCs, first execution), not full PC traces.
4. **Renode writes image metadata per test**: per machine/CPU the loaded ELFs (local path, content hash, load offset).
   Post-processor resolves DWARF once per hash, attributes ranges to images, reports unattributed share.
   Reuse `coverage.py`/`dwarf.py` from `tools/execution_tracer/execution_tracer/`.
5. **Interface:** flag + config file. Config (`tia.yaml`) maps DWARF path prefixes → Teamscale projects (+ strip,
   ignore). One report per Teamscale project. Upload stays outside the runner (`teamscale-upload` in CI).
6. **Runner-agnostic test boundaries:** Renode monitor commands like `TestwiseCoverage.BeginTest "<uniformPath>"` /
   `EndTest "<result>"`; front-ends: `renode-test` (Test Setup/Teardown in `renode-keywords.robot`), a twister
   integration, and a ZMK `run-test.sh` port.
7. **Retries:** one test entry, result of last attempt, union of coverage.
8. **Selection round-trip:** a way to run an explicit list of uniformPaths (e.g. `renode-test --tests-from <file>`;
   twister/ZMK equivalents).
9. **Demo targets (two-board matrix):**
   - Upstream Zephyr via twister on `riscv32_virtual` — scale, hours of runtime. Test unit = ztest case
     (`<board>/<scenario>/<suite>/<case>`), coverage split at ztest UART markers inside Renode; suite setup →
     `__setup__` entry. Execution stays at scenario granularity with ztest case filtering.
   - **ZMK** (zmkfirmware/zmk) on `nrf52840dk` — "my product code, my board". Test unit = snapshot test
     (one ELF per test, whole engine in every ELF → static linking can't discriminate). Port `run-test.sh` to Renode
     (no twister). Teamscale projects: ZMK + ZMK's Zephyr fork.
   - TFLM (bluepill, renode-test) only as an end-to-end smoke test.
10. **Evaluation:** mutation testing for statistics + reversed bugfix commits for 2–3 real stories. Compare run-all,
    static selection (Zephyr: `scripts/ci/test_plan.py`; ZMK: run-all), TIA with prioritization. Show "failures found
    over elapsed time". Measure build and run time separately (TIA saves west builds too).

## Presentation must address
- Why Renode vs `native_sim` + gcov: coverage of the unmodified production ELF, real arch, peripheral models.
- Non-code changes (devicetree, Kconfig, `prj.conf`, overlays, CMake, `testcase.yaml`): rule "build-config change →
  affected scenarios impacted / run all for that board".
- Baseline refresh via periodic full runs.
- Public, reproducible demo repo.

## First step (knock-out check)
Verify ZMK builds and runs its mock-kscan tests on `nrf52840dk` in Renode without USB (settings, PM, USB HID).

## Existing prototype (`testwise-coverage-transfer/`, `coverage-demo/`, untracked `tools/execution_tracer/*`)
- Drop: per-test `Start/Stop Testwise Trace` keywords (CPU doesn't exist at setup), `.resc` regex trace discovery,
  single `--auto-lcov-binary`, `project/architecture/test` uniformPath, `coverage-demo/generate_testwise_coverage.py`
  (hardcoded, third DWARF parser, always PASSED), `fix_and_run_tests.py`.
- Keep/salvage: line-set → range-string conversion, file-index merge logic, report JSON shape (version 2).
