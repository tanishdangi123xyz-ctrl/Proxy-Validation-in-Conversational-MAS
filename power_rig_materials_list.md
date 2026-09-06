# Power measurement rig: materials list

Everything needed to build the external power measurement rig end to end,
from the Jetson's power line through the shunt to the ESP32's own ADC pins
and the laptop. Grouped by what each part does. Specs below match exactly
what `firmware/esp32/masenergy_sampler/masenergy_sampler.ino` expects; if
you substitute a part with different specs, the firmware's constants
(`R_SHUNT_OHMS`, `R_DIVIDER_TOP_OHMS`, `R_DIVIDER_BOTTOM_OHMS`) need to
change to match.

## What changed on 2026-09-06, and why this list is shorter now

The design used to read an INA226 sensor chip over I2C. It now reads a
shunt and a resistor-divided bus voltage directly on two of the ESP32's own
ADC pins, to get a much higher sample rate than the I2C link could support,
and because sourcing a suitable INA226 breakout in India had become a real
blocker (every hobbyist board found was either out of stock or shipped
with an undocumented fixed onboard shunt, not the external, known-value
shunt this project's math depends on). See `CHANGES.md`, 2026-09-06, for
the full reasoning, including what this trades away in measurement
accuracy and how the firmware compensates for it.

The practical effect on this list: the INA226 breakout board is gone
entirely, and the shunt changed from 20 mOhm to 100 mOhm. Two small,
cheap, easy-to-find resistors (a voltage divider) replace it for reading
the bus voltage. This removes what had been the single blocking item on
the previous version of this list.

## Already have, confirmed working

- ESP32 dev board. Confirmed as an ESP32-D0WD-V3, flashed twice now: first
  with the INA226-based firmware (2026-09-02), then with the current
  ADC-based firmware (2026-09-06). Both times the frame stream and the
  trigger_pulse_n join-key logic validated cleanly on real hardware; the
  ADC-based version's own achieved rate measured at 755.2 frames/sec, not
  comparable to the retired version's 1499 frames/sec since the two
  designs are bottlenecked by different things. See `CHANGES.md`,
  2026-09-06.
- USB cable, ESP32 to laptop.
- `arduino-cli` toolchain and `pyserial`, installed and working.

## Power path: splicing the shunt into the Jetson's supply line

This is the part of the build that touches the Jetson's actual power line,
so get the connectors right for your specific hardware before cutting or
soldering anything.

- **DC pigtail cable, male end.** A short cable with a bare-wire end on one
  side and a plug on the other, matching what the Jetson's power port
  needs (5.5mm OD x 2.5mm ID, confirmed from NVIDIA's own carrier board
  documentation). This becomes the "Jetson side" of the spliced line.
- **DC pigtail cable, female end.** Same idea, but a socket instead of a
  plug, matching your power supply's connector. This becomes the "PSU
  side."
- Confirm the pigtail plug/socket size and polarity against your specific
  power supply before ordering; a 5.5x2.1mm connector (the more common
  size for generic 12V gear) looks similar but is not the same fit as the
  Jetson's 5.5x2.5mm jack and can make an unreliable center-pin contact.

## The shunt: still one item to source, now simpler to find

- **100 mOhm shunt resistor, low-side placement, rated at least 1W.** The
  firmware is built around exactly this value (`R_SHUNT_OHMS = 0.100`); a
  different resistance needs a firmware constant change to match. Unlike
  the previous 20 mOhm design, this one does NOT need to be a 4-terminal
  Kelvin-tapped part: since the ESP32 reads the shunt directly on a single
  ADC pin rather than through a chip with true differential Kelvin inputs,
  the accuracy benefit of a 4-terminal part is smaller relative to the
  ESP32 ADC's own error budget (see `CHANGES.md`, 2026-09-06), and a
  standard 2-terminal through-hole power resistor at this value is far
  easier to find, including on ordinary Indian hobbyist electronics sites.
  Rated current headroom: the design targets up to roughly 2-3A through
  the shunt, so 1W covers that with margin (I^2 x R at 3A through 100 mOhm
  is 0.9W).
- **LOW-SIDE placement matters and is a wiring change, not just a part
  choice.** The shunt goes in the ground return path (between the Jetson's
  GND and the power supply's GND), not the positive 19V line the original
  circuit diagram showed. See the firmware's own header comment (LOW-SIDE
  SHUNT PLACEMENT) for exactly why; getting this wrong means the ESP32's
  ADC pin would be reading a signal riding on top of 19V, which it cannot
  do.

## The bus voltage divider: two resistors, replaces the INA226 entirely

- **Two resistors forming a voltage divider**: 100 kOhm and 18 kOhm, both
  standard values any electronics supplier stocks. Together they bring the
  Jetson's ~19V supply rail down to a safe ~2.9V for the ESP32's second ADC
  pin to read directly. Tolerance matters here more than for most passive
  parts in this build: a 1% or better tolerance on both resistors keeps the
  bus voltage reading accurate; a rough 5% or 10% resistor could introduce
  a real, silent, calibration-defeating error, since nothing in this design
  independently checks the divider ratio against a known reference the way
  the shunt's own zero-offset calibration checks itself.
- These two resistors and the 330 ohm trigger resistor below are the whole
  replacement for what used to be a single INA226 breakout board purchase;
  together they cost a small fraction of what the INA226 board itself did,
  and none of them have had a stock or sourcing problem the way the INA226
  did.

## Connecting everything

- **330 ohm resistor**, in series between the Jetson's trigger GPIO output
  and the ESP32's `PIN_TRIGGER` (GPIO4). Unchanged from the original
  design. Any generic 330 ohm resistor works, no special tolerance needed
  (unlike the two divider resistors above).
- **Hookup wire**, roughly 18 to 20 AWG for the power-carrying connections
  (shunt to PSU, shunt to Jetson), thinner gauge is fine for the low-current
  ADC and signal lines (shunt to ESP32, divider to ESP32, trigger line). To
  connect everything on the sense/signal side.
- **Jumper wires** (male-male and male-female, a small assortment), useful
  for prototyping the shunt-to-ESP32, divider-to-ESP32, and trigger
  connections without soldering first, so the circuit can be checked
  working before anything is made permanent.
- **Breadboard**, to hold the divider resistors and the ESP32 wiring
  cleanly during prototyping and testing, rather than loose wire-to-wire
  twists (a common source of flaky, intermittent readings, and now a more
  important concern than before: the ESP32's own ADC pins have no built-in
  differential rejection the way the old INA226 chip did, so a poor
  connection here shows up directly as measurement error, not just noise).
- **Solder, soldering iron, and heat shrink tubing**, for making the
  power-path splices (shunt in series with the ground return line)
  permanent and insulated once the rig is confirmed working on the
  breadboard. Not optional for the power-carrying joints specifically; a
  twisted or taped connection in that path is a real fire and reliability
  risk at sustained current, unlike the low-current signal side which can
  stay on jumpers indefinitely if convenient.

## Tools worth having on hand, not consumed by the build

- **Wire strippers**, for prepping the pigtail cable ends and hookup wire.
- **A multimeter**, to confirm continuity and check for shorts on the
  power-path splice before ever connecting it to the Jetson. Also useful
  to independently verify the divider's actual output voltage once wired
  (measure across the 18 kOhm resistor directly with the Jetson powered)
  as a sanity check against `host/live_monitor.py`'s reported bus voltage,
  and to confirm the two divider resistors' real measured values if their
  tolerance is in question.
- A small Phillips/flathead screwdriver set is no longer needed for this
  build specifically, since there is no INA226 breakout board with screw
  terminals in this design anymore; keep one on hand only if some other
  connector in your specific build calls for it.

## What you do not need

Worth stating explicitly, since it is easy to over-buy: no WiFi or
Bluetooth module (the firmware deliberately never initializes either), no
separate microcontroller besides the ESP32, no INA226 or any other
external sensor chip, no 4-terminal Kelvin shunt (a standard 2-terminal
through-hole resistor is sufficient for this design, see The Shunt
section above for why).

## What is left to source

Just the 100 mOhm shunt resistor itself. The two divider resistors (100k
and 18k) and the 330 ohm trigger resistor are the kind of part sold by
literally every general electronics supplier, in India or otherwise, in
packs of dozens for a few rupees, so they are not worth tracking as
separate sourcing items.

`host/live_monitor.py` already confirmed the board-only half of the build
on 2026-09-06, with nothing wired to the ADC pins yet: `startup_
calibration_not_run` clears a few hundred milliseconds after the ESP32
boots, exactly as designed, and `trigger_pulse_n` increments correctly on
a hand-toggled GPIO edge. What is left, once the shunt and the two divider
resistors are in hand and wired per the firmware's own header comment
(low-side shunt in the ground return path, divider across the bus, both
feeding their own ADC pin), is powering on the Jetson and watching
`live_monitor.py` for sane, non-faulted current and bus voltage readings,
then cross-checking the divider's actual output with a multimeter before
trusting it unattended for a real campaign.
