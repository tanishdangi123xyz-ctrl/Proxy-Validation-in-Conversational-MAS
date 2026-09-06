/*
 * smoke_test.ino
 *
 * Not part of the measurement rig. A five-line sketch to run BEFORE
 * masenergy_sampler.ino, so a failure at that stage is isolated to "the
 * toolchain, the board, or the cable" rather than confused with a bug in
 * the real firmware. If this blinks the onboard LED and prints to serial,
 * the ESP32 core install, the port, the cable, and arduino-cli's
 * compile/upload/monitor path are all confirmed working, and any problem
 * seen when flashing masenergy_sampler.ino next is a real problem with
 * that file, not the environment around it.
 *
 * Most ESP32 DevKit boards have an onboard LED on GPIO 2, but not all of
 * them (some run it on GPIO 5, GPIO 33, or not at all). If the LED does
 * not blink but the serial output below still appears in the monitor,
 * that is still a full pass, the serial print is the real proof; the LED
 * is a bonus visual.
 *
 *     arduino-cli compile --fqbn esp32:esp32:esp32 firmware/esp32/smoke_test
 *     arduino-cli upload -p /dev/cu.usbserial-XXXX --fqbn esp32:esp32:esp32 firmware/esp32/smoke_test
 *     arduino-cli monitor -p /dev/cu.usbserial-XXXX -c baudrate=115200
 */

static const uint8_t LED_PIN = 2;
static uint32_t counter = 0;

void setup() {
  Serial.begin(115200);
  pinMode(LED_PIN, OUTPUT);
  delay(500);  // gives the serial monitor time to attach before the first line
  Serial.println("smoke_test: boot ok");
}

void loop() {
  digitalWrite(LED_PIN, counter % 2);
  Serial.printf("smoke_test: alive, tick %lu\n", (unsigned long)counter);
  counter++;
  delay(500);
}
