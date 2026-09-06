/*
 * ina226-stub.chip.c
 *
 * A Wokwi custom I2C chip that stands in for the real INA226 so
 * masenergy_sampler.ino can be simulated end to end in the browser, with no
 * physical ESP32 or INA226 required. This is NOT a claim that Wokwi's I2C
 * timing or electrical behaviour exactly reproduces the real chip; it exists
 * to prove the firmware's I2C transaction sequencing, register math, and
 * fault handling actually execute correctly against something that answers
 * like an INA226 would, catching the class of bug a read-through of the
 * .ino file cannot: a wrong register address, a byte-order mistake in a
 * 16-bit write, a calibration readback comparison that never actually
 * matches, an off-by-one in how many bytes a read transaction returns.
 *
 * WHAT IS SIMULATED AND WHY THOSE PIECES SPECIFICALLY
 *
 * masenergy_sampler.ino's own logic, read closely, touches exactly four
 * things on the real chip: a soft reset (writing 0x8000 to REG_CONFIG), a
 * config write it never reads back, a calibration write it DOES read back
 * and compare (g_cal_ok in setup()), and repeated reads of
 * REG_SHUNT_VOLTAGE/REG_BUS_VOLTAGE in the sample loop. This stub
 * implements exactly those four register addresses and nothing else,
 * deliberately: a stub that also modelled the alert/mask register or
 * power-on defaults for registers the firmware never touches would be
 * simulating fidelity the test does not need and could hide behind.
 *
 * shunt_raw and bus_raw are derived from two Wokwi chip controls
 * (busMillivolts, shuntMicrovolts) using the real INA226 LSB sizes from the
 * datasheet (2.5uV/LSB shunt, 1.25mV/LSB bus), the same constants
 * host/capture.py uses to decode the real chip's registers, so a value set
 * on the slider and a value read back out of the firmware's frames over
 * serial should agree, which is itself part of what running this proves.
 *
 * failReads, when set to 1, makes every register read on this chip NAK the
 * transaction (return false from the connect handler), exercising
 * FAULT_I2C_SHUNT/FAULT_I2C_BUS and FAULT_CAL_UNSET in the firmware exactly
 * the way a real disconnected or dead chip would.
 *
 * See https://docs.wokwi.com/chips-api/getting-started and
 * https://docs.wokwi.com/chips-api/i2c for the platform contract this file
 * is written against.
 */

#include "wokwi-api.h"
#include <stdio.h>
#include <stdint.h>

typedef enum {
  I2C_STATE_IDLE,
  I2C_STATE_WAIT_REG,       // next byte in is a register address
  I2C_STATE_WAIT_DATA_HI,   // next byte in is the MSB of a 16-bit write
  I2C_STATE_WAIT_DATA_LO,   // next byte in is the LSB of a 16-bit write
} i2c_write_state_t;

typedef struct {
  i2c_dev_t i2c;

  uint8_t reg_addr;
  i2c_write_state_t write_state;
  uint8_t pending_hi;

  uint16_t reg_config;
  uint16_t reg_calibration;

  // For a register READ, the two bytes are sent MSB first; read_byte_index
  // tracks which of the two bytes is next.
  uint8_t read_byte_index;
  uint16_t read_value_latched;

  pin_t pin_bus_mv_ctrl;
  attr_t attr_bus_mv;
  attr_t attr_shunt_uv;
  attr_t attr_fail;
} chip_state_t;

static const uint8_t REG_CONFIG = 0x00;
static const uint8_t REG_SHUNT_VOLTAGE = 0x01;
static const uint8_t REG_BUS_VOLTAGE = 0x02;
static const uint8_t REG_CALIBRATION = 0x05;

static const float SHUNT_VOLT_LSB_UV = 2.5f;   // microvolts per LSB
static const float BUS_VOLT_LSB_MV = 1.25f;    // millivolts per LSB

static int16_t compute_shunt_raw(chip_state_t *chip) {
  float shunt_uv = attr_read(chip->attr_shunt_uv);
  return (int16_t)(shunt_uv / SHUNT_VOLT_LSB_UV);
}

static uint16_t compute_bus_raw(chip_state_t *chip) {
  float bus_mv = attr_read(chip->attr_bus_mv);
  uint32_t raw = (uint32_t)(bus_mv / BUS_VOLT_LSB_MV);
  if (raw > 0x7FFF) {
    raw = 0x7FFF;  // bus voltage register is unsigned but 15 significant bits
  }
  return (uint16_t)raw;
}

static bool on_i2c_connect(void *user_data, uint32_t address, bool read) {
  chip_state_t *chip = (chip_state_t *)user_data;
  if (attr_read(chip->attr_fail)) {
    // Simulates a dead/disconnected chip: NAK every transaction, which
    // maps to ina226_read16()/ina226_write16() returning false in the
    // firmware, and should set FAULT_I2C_SHUNT / FAULT_I2C_BUS /
    // FAULT_CAL_UNSET depending on which call this was.
    return false;
  }
  if (!read) {
    // A write transaction always starts with the register address byte.
    chip->write_state = I2C_STATE_WAIT_REG;
  } else {
    // A read transaction reports whatever register address is currently
    // selected in reg_addr. ina226_read16() in the firmware issues a write
    // of the register address with Wire.endTransmission(false) (no STOP),
    // then Wire.requestFrom() to read the value back, i.e. a repeated
    // START. Whether the Wokwi I2C simulation represents that as one held
    // connection or as a fresh connect() call with read now true is not
    // documented and was not independently confirmed before writing this
    // stub; reg_addr is deliberately stored on chip_state_t rather than in
    // any per-transaction-local state specifically so this works correctly
    // either way, latching from whatever reg_addr currently holds rather
    // than assuming a particular transport-level boundary.
    chip->read_byte_index = 0;
    switch (chip->reg_addr) {
      case REG_SHUNT_VOLTAGE:
        chip->read_value_latched = (uint16_t)compute_shunt_raw(chip);
        break;
      case REG_BUS_VOLTAGE:
        chip->read_value_latched = compute_bus_raw(chip);
        break;
      case REG_CALIBRATION:
        chip->read_value_latched = chip->reg_calibration;
        break;
      case REG_CONFIG:
        chip->read_value_latched = chip->reg_config;
        break;
      default:
        // An address this stub does not model. Returning 0 rather than
        // failing the connect lets a firmware bug that reads the wrong
        // register surface as a suspicious-looking value instead of a
        // silent NAK that could be mistaken for a wiring problem.
        chip->read_value_latched = 0x0000;
        break;
    }
  }
  return true;
}

static uint8_t on_i2c_read(void *user_data) {
  chip_state_t *chip = (chip_state_t *)user_data;
  uint8_t byte = (chip->read_byte_index == 0)
                     ? (uint8_t)(chip->read_value_latched >> 8)
                     : (uint8_t)(chip->read_value_latched & 0xFF);
  chip->read_byte_index++;
  return byte;
}

static bool on_i2c_write(void *user_data, uint8_t data) {
  chip_state_t *chip = (chip_state_t *)user_data;
  switch (chip->write_state) {
    case I2C_STATE_WAIT_REG:
      chip->reg_addr = data;
      chip->write_state = I2C_STATE_WAIT_DATA_HI;
      break;
    case I2C_STATE_WAIT_DATA_HI:
      chip->pending_hi = data;
      chip->write_state = I2C_STATE_WAIT_DATA_LO;
      break;
    case I2C_STATE_WAIT_DATA_LO: {
      uint16_t value = ((uint16_t)chip->pending_hi << 8) | data;
      if (chip->reg_addr == REG_CONFIG) {
        chip->reg_config = value;
        if (value == 0x8000) {
          // Soft reset. The firmware always follows this with a real
          // config write, but modelling the reset clearing calibration
          // too (as the real chip's datasheet specifies) is what makes
          // g_cal_ok in the firmware meaningfully test something: if the
          // firmware ever stopped writing calibration after a reset, this
          // stub would then read back 0 and g_cal_ok would correctly go
          // false.
          chip->reg_calibration = 0;
        }
      } else if (chip->reg_addr == REG_CALIBRATION) {
        chip->reg_calibration = value;
      }
      // Any register this stub does not model (there are none the
      // firmware writes to besides these two) is silently accepted and
      // dropped, matching on_i2c_connect's read-side default of 0 for an
      // unmodelled address.
      chip->write_state = I2C_STATE_WAIT_REG;  // subsequent bytes would be
                                                // additional writes if the
                                                // controller kept going,
                                                // matching the real chip's
                                                // auto-increment behaviour
                                                // closely enough for this
                                                // firmware, which never
                                                // does that.
      break;
    }
    default:
      break;
  }
  return true;
}

void chip_init(void) {
  chip_state_t *chip = malloc(sizeof(chip_state_t));
  chip->write_state = I2C_STATE_IDLE;
  chip->reg_addr = 0;
  chip->reg_config = 0;
  chip->reg_calibration = 0;
  chip->read_byte_index = 0;
  chip->read_value_latched = 0;

  chip->attr_bus_mv = attr_init("busMillivolts", 19000);
  chip->attr_shunt_uv = attr_init("shuntMicrovolts", 800);
  chip->attr_fail = attr_init("failReads", 0);

  i2c_config_t i2c_config = {
      .user_data = chip,
      .address = 0x40,  // INA226 default address, A1=A0=GND
      .scl = pin_init("SCL", INPUT),
      .sda = pin_init("SDA", INPUT),
      .connect = on_i2c_connect,
      .read = on_i2c_read,
      .write = on_i2c_write,
      .disconnect = NULL,
  };
  chip->i2c = i2c_init(&i2c_config);

  printf("ina226-stub: initialised, address 0x40\n");
}
