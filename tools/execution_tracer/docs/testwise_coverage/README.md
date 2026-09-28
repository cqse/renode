# Testwise coverage for Teamscale Test Impact Analysis

Goal: export **testwise coverage** from Renode test runs, meaning which guest-firmware source lines each individual
test executed, in Teamscale's
[testwise-coverage format](https://docs.teamscale.com/tutorial/generating-testwise-coverage/). With it,
[Test Impact Analysis (TIA)](https://docs.teamscale.com/howto/improving-test-efficiency/) runs only the tests affected by
a code change.

## Status

This branch contains a **prototype**. It works for a single known firmware, but it is **not** the target design:

- tests must be edited by hand (`Start/Stop Testwise Trace` in each test's setup and teardown);
- traces are discovered by regex-parsing a `.resc` file, not from what actually ran;
- one ELF is used for the whole run;
- full ordered PC traces are recorded (large and slow);
- `uniformPath` is `project/architecture/test`.

The agreed target design is in [decisions.md](decisions.md):

- a Renode-side global switch that records executed address ranges plus loaded-image metadata per test;
- runner-agnostic test boundaries;
- a post-processor that writes one report per Teamscale project from a `tia.yaml` mapping.

[zmk-on-renode-findings.md](zmk-on-renode-findings.md) documents the demo target: ZMK snapshot tests running on
`nrf52840dk` in Renode.

## Prototype components

| File | Purpose |
|---|---|
| `execution_tracer_keywords.py` | Robot keywords `Start Testwise Trace <cpu> <test> <dir>` / `Stop Testwise Trace <cpu>`: one binary PC trace per test |
| `testwise_coverage_listener.py` | Robot listener (API v3) that writes each test's name, result and duration to a JSON manifest |
| `generate_testwise_coverage.py` | Reads the `CreateExecutionTracing` lines from a `.resc` file, converts the traces to LCOV via `execution_tracer_reader.py coverage`, and writes testwise-coverage JSON (`generate`); merges reports (`merge`) |

## Using the prototype

Wire per-test tracing into a suite. The machine must already exist when `Start Testwise Trace` runs, so call it after
the machine has been created (not in a `[Setup]` that runs before `Reset Emulation`):

```robotframework
*** Settings ***
Library    tools/execution_tracer/execution_tracer_keywords.py

*** Test Cases ***
My Test
    Execute Command         include @scripts/single-node/my_platform.resc
    Start Testwise Trace    cpu    ${TEST_NAME}    /path/to/traces
    # ... test body ...
    [Teardown]              Stop Testwise Trace    cpu
```

Run with the listener, then generate the report:

```bash
renode-test my_tests.robot \
    --listener tools/execution_tracer/testwise_coverage_listener.py:/path/to/manifest.json

python3 tools/execution_tracer/generate_testwise_coverage.py generate my_test.resc \
    --output-dir out --auto-lcov --auto-lcov-binary firmware.elf \
    --project my_project --architecture my_arch \
    --manifest /path/to/manifest.json --partial

python3 tools/execution_tracer/generate_testwise_coverage.py merge out/*.json --output merged.json
```

Upload the JSON to Teamscale via its testwise-coverage upload API.
