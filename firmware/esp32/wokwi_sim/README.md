# Wokwi simulation of `masenergy_sampler.ino`

Runs the real firmware from `../masenergy_sampler.ino` in the browser,
against a custom simulated INA226 (`ina226-stub.chip.c` /
`ina226-stub.chip.json`) and a pushbutton standing in for the Jetson's
trigger line. No ESP32, INA226, or serial cable required. This is not a
claim that Wokwi's simulation is electrically identical to the real parts,
see the "What this does and does not prove" note at the bottom.

## Setup (five minutes, browser only, free account)

1. Go to `wokwi.com/projects/new/esp32` and sign in (a free Wokwi account
   is enough).
2. In the file panel on the left, delete the default `sketch.ino` content
   and paste in the contents of `../masenergy_sampler.ino` (the real
   firmware, unmodified).
3. Still in the file panel, click "+" to add a new file, name it
   `diagram.json`, and replace its contents with this folder's
   `diagram.json`.
4. Add another new file named `ina226-stub.chip.c`, paste this folder's
   file of the same name.
5. Add another new file named `ina226-stub.chip.json`, paste this folder's
   file of the same name.
6. Click the green "Play" (Start simulation) button. Wokwi compiles the
   `.ino` against the real ESP32 Arduino core server-side; a compile error
   here is a real error in the firmware, not a simulation artifact.

## What to watch

Open the Serial Monitor tab (bottom panel). If `g_cal_ok` came back false
you would see it indirectly: `FAULT_CAL_UNSET` would appear in every
frame's fault byte, decodable by eye is hard from raw bytes, so the
quickest sanity check is the Wokwi "Chips console" tab (select the
`ina226-stub` chip, then Console), which prints `ina226-stub: initialised,
address 0x40` once at boot from the stub's own `chip_init()`. Seeing that
line confirms the I2C wiring is correct enough for the chip to have been
addressed and configured at all before you go looking at frame content.

To generate trigger pulses, click the pushbutton part in the diagram (or
press and release the `t` key, per its `key` attribute in diagram.json).
Each press/release is one rising/falling edge on GPIO 4, exactly like the
Jetson driving the real trigger line, and `g_pulse_n` in the firmware
increments on the press.

The two sliders on the `ina226-stub` chip (click the chip to reveal its
control panel) let you change the simulated bus voltage and shunt voltage
live, and the frames streamed over serial should track those changes,
using the exact same 2.5uV/1.25mV LSB math as `host/capture.py`. The third
slider, `failReads`, forces every I2C transaction to fail when set to 1,
which is how to check that `FAULT_I2C_SHUNT`/`FAULT_I2C_BUS` actually get
set and that shunt_raw/bus_raw actually go to 0 (not a stale prior value)
in that case, exercising a code path that is otherwise very hard to
trigger deliberately.

## Capturing the real stream with host/capture.py

Wokwi's Serial Monitor shows bytes as text, which is not useful for
binary frames. To actually run `host/capture.py` against this simulation
rather than eyeballing bytes, use Wokwi's serial-over-WebSocket bridge (a
paid Wokwi CI/Pro feature at the time this was written) or, more simply,
Wokwi's VS Code extension, which exposes the simulated board as a real
local serial port your OS can open. That path needs `wokwi.toml` alongside
a locally compiled `.bin`/`.elf`, which is a different setup than the
paste-and-play browser flow above; see
`https://docs.wokwi.com/vscode/project-config` if that route is wanted
later. The browser flow above is for validating the firmware's own logic
by eye and by the chip's fault-injection sliders, not for feeding a real
binary stream into `host/capture.py`.

## What this does and does not prove

Proves: the firmware compiles clean against the real ESP32 Arduino core,
the I2C register sequence (soft reset, config write, calibration
write-then-readback, repeated shunt/bus reads) executes correctly against
something that answers like an INA226's register map, the trigger
debounce and pulse-counting logic responds correctly to real edges, and
the fault-bit paths (`FAULT_I2C_SHUNT`, `FAULT_I2C_BUS`, `FAULT_CAL_UNSET`)
are reachable and behave as documented.

Does not prove: real I2C bus electrical behaviour (rise times, pull-up
sizing, Fast Mode timing margins) or real timing precision at 1.5 kHz
under Wokwi's simulated CPU, since Wokwi's own timing fidelity relative to
real silicon is not something this project has independently verified;
whether the real Vishay/Susumu INA226 breakout responds identically to
every corner case this stub implements (the stub only models the four
registers the firmware actually touches, see the header comment in
`ina226-stub.chip.c`); or anything about the real shunt, real Jetson GPIO
edges, or real USB serial throughput over a real cable for a real
multi-day campaign. Checklist item 12 in the main README stays unchecked
until those are verified on real hardware.
