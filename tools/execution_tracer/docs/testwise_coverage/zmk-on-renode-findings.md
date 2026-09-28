# Findings: ZMK snapshot tests on nrf52840dk in Renode

Knock-out check run on macOS (arm64), Renode 1.16.1, ZMK `9ebbeff0` (Zephyr fork `v4.1.0+zmk-fixes`),
2026-09-24/25.

> The `variant/`, `scripts/` and `robot/` files referenced below belong to the separate `zmk-renode-tia` bundle
> (Docker image, build/run scripts, Robot suite generator); they are not part of this repository.

## Result

**Passed.** ZMK builds for `nrf52840dk/nrf52840/zmk` without USB/BLE, boots in Renode's `nrf52840` platform, and its
snapshot tests run unmodified. A sample of one test per area gave **23/24 passing snapshot comparisons**; the full
suite (248 tests) has not been run yet.

| Area sample | Result |
|---|---|
| caps-word, combo, conditional-layer, encoders, gresc, hold-tap, key-repeat, keypress, keytoggle, macros, matrix-input, mod-morph, modifiers, momentary-layer, none, pointing, sticky-keys, studio, tap-dance, to-layer, toggle-layer, transparent, wpm | PASS |
| backlight/basic | FAIL (see below) |

## What the Renode variant needs (all in `variant/`)

| Problem | Symptom | Fix |
|---|---|---|
| No kscan in the test build | — | `mock_kscan.overlay`: `zmk,kscan-mock` node (as in `native_sim_zmk_test_mock.dts`) with `exit-after` |
| Renode has no QSPI model | Firmware spins forever in `nrf_event_check` polling `QSPI:EVENTS_READY` (0x40029100); 3 s virtual time took ~2:40 wall | Disable **both** `&qspi` and its child `&mx25r64` (child has no status and stays okay otherwise) + `CONFIG_NORDIC_QSPI_NOR=n` |
| ZMK defaults `USB_DEVICE_STACK=y` on nRF (`default y if HAS_HW_NRF_USBD`), Renode has no USBD model | — | `CONFIG_ZMK_USB=n`, `CONFIG_USB_DEVICE_STACK=n` |
| ZMK only wires a console for USB CDC logging | UART log empty | `CONFIG_SERIAL/UART_CONSOLE/LOG_BACKEND_UART=y`, `LOG_MODE_IMMEDIATE=y` |
| Test-specific `native_sim.conf` is picked up by board name only on native_sim | e.g. backlight/wpm/pointing/matrix-input features missing, empty snapshot | Pass `<test>/native_sim.conf` via `EXTRA_CONF_FILE` (done in `build-tests.sh`) |
| `LOG_MODE_IMMEDIATE` formats logs on the caller's stack; board has MPU stack guard; default stacks 1 KiB main / 2 KiB workqueue | `MPU FAULT` / Data Access Violation in `__d_vfprintf` via `uarte_nrfx_poll_out` (hold-tap/balanced/7-positional/on-release-no-trigger) | `CONFIG_MAIN_STACK_SIZE=4096`, `CONFIG_SYSTEM_WORKQUEUE_STACK_SIZE=4096` |
| `events.patterns` use GNU sed (`\|`), `run-test.sh` uses `diff -Z` | Empty filtered log / spurious diff on macOS | Linux: non-issue. Library prefers `gsed`, compares ignoring trailing whitespace |

End-of-test marker: the mock kscan driver calls `exit(0)` after the last event; on the nRF build picolibc prints a
line `exit` on the UART. The Robot suite waits for `^exit$`.

## Open failure: backlight/basic

`tests/backlight/behavior_keymap.dtsi` puts the backlight LEDs on `gpio0` pins 0 and 1. On nRF52840 these are the
32 kHz crystal pins (XL1/XL2) and are reserved on nrf52840dk, so `gpio_pin_configure` asserts at boot
(`ASSERTION FAIL [(cfg->port_pin_mask & ...)]`, gpio.h:1020). The test also enables `CONFIG_GPIO_EMUL`, which is
native_sim infrastructure. Fix idea: a per-area overlay (`variant/tests/backlight.overlay`) remapping the LEDs to free
pins (e.g. DK LEDs P0.13/P0.14) — or exclude the backlight area. Expect the other `backlight/*` tests to fail the same way.

## Docker end-to-end check (macOS arm64 host, Linux container)

The bundle was run end to end in the Docker image (`setup-workspace.sh`, `build-tests.sh hold-tap/balanced`,
`run-tests.sh`): **22/22 PASS** after the stack-size fix above (21/22 before it). Renode run for all 22 tests with 3
shards: 45 s wall.

**Builds in the Linux container are much faster than native macOS builds on the same machine**: avg 61 s per build
with 3 parallel builds x 4 ninja jobs, 25 s for a single build with 8 jobs, against 3–7 min natively on macOS. The slow
native times were mostly a macOS effect (process spawning, filesystem). Expect a big Linux machine to build all 248
tests in well under an hour.

## Timing observations (native macOS, 12 cores)

- **Renode per test (Robot, one instance per shard, stop at `exit`)**: 0.6–8 s per test; 24 tests in 53 s wall with 2
  shards, including Renode startup. Emulated time per test is only ~0.1–0.4 s.
- Earlier fixed `RunFor "00:00:10"` approach: ~13 s per test (startup + fixed window) — replaced.
- **Builds dominate.** One west build ≈ 3–7 min on the Mac; 4 unthrottled parallel builds took 18–65 min *each*
  (ninja oversubscription). `build-tests.sh` caps `NINJA_JOBS` per build.
- ccache helps less than expected: each test's keymap changes the generated devicetree header, which nearly every
  Zephyr file includes → mostly cache misses. Parallelism is the lever.
- Presentation point: with one ELF per test, TIA saves *builds*, not just test runs.
