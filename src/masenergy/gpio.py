"""GPIO character-device driver, v2 ABI.

The trigger line is the entire basis of per-call energy attribution, so the
cost of raising it has to be small, constant, and knowable. Everything here
exists to make an edge as close to a single syscall as Python allows: the line
is requested once at construction and the descriptor held for the run, and the
ioctl payload is allocated once and mutated in place, so high() and low()
allocate nothing and touch no filesystem.

The legacy /sys/class/gpio interface is deliberately not used. It is deprecated
and scheduled for removal, it costs an open/write/close per edge, and its
latency varies with page cache state. The character device is one ioctl on an
open descriptor.

No project imports. This module knows about Linux, not about the experiment,
which is what lets it be exercised against known ABI constants rather than
against a Jetson.

Standard library only, Python 3.10 compatible.
"""

import ctypes
import fcntl
import os
from pathlib import Path

GPIO_MAX_NAME_SIZE = 32
GPIO_V2_LINES_MAX = 64
GPIO_V2_LINE_NUM_ATTRS_MAX = 10

GPIO_V2_LINE_FLAG_INPUT = 1 << 2
GPIO_V2_LINE_FLAG_OUTPUT = 1 << 3

_IOC_NONE, _IOC_WRITE, _IOC_READ = 0, 1, 2
_GPIO_IOC_MAGIC = 0xB4

DEV_ROOT = Path("/dev")
CHIP_GLOB = "gpiochip*"


class GpioError(RuntimeError):
    """Raised when a line cannot be opened, configured or driven."""


def _ioc(direction, nr, size):
    """Encode an ioctl request number the way the kernel's _IOC macro does.

    Derived from ctypes.sizeof rather than hard-coded, which turns a wrong
    structure definition into an immediate ENOTTY from the kernel instead of a
    correctly numbered ioctl carrying a misaligned payload. The first is a
    refusal at the first edge; the second is a campaign of plausible pulses.
    """
    return (direction << 30) | (size << 16) | (_GPIO_IOC_MAGIC << 8) | nr


class _LineValues(ctypes.Structure):
    _fields_ = [("bits", ctypes.c_uint64), ("mask", ctypes.c_uint64)]


class _LineAttributeValue(ctypes.Union):
    _fields_ = [("flags", ctypes.c_uint64), ("values", ctypes.c_uint64),
                ("debounce_period_us", ctypes.c_uint32)]


class _LineAttribute(ctypes.Structure):
    _fields_ = [("id", ctypes.c_uint32), ("padding", ctypes.c_uint32),
                ("value", _LineAttributeValue)]


class _LineConfigAttribute(ctypes.Structure):
    _fields_ = [("attr", _LineAttribute), ("mask", ctypes.c_uint64)]


class _LineConfig(ctypes.Structure):
    _fields_ = [
        ("flags", ctypes.c_uint64),
        ("num_attrs", ctypes.c_uint32),
        ("padding", ctypes.c_uint32 * 5),
        ("attrs", _LineConfigAttribute * GPIO_V2_LINE_NUM_ATTRS_MAX),
    ]


class _LineRequest(ctypes.Structure):
    _fields_ = [
        ("offsets", ctypes.c_uint32 * GPIO_V2_LINES_MAX),
        ("consumer", ctypes.c_char * GPIO_MAX_NAME_SIZE),
        ("config", _LineConfig),
        ("num_lines", ctypes.c_uint32),
        ("event_buffer_size", ctypes.c_uint32),
        ("padding", ctypes.c_uint32 * 5),
        ("fd", ctypes.c_int32),
    ]


class _ChipInfo(ctypes.Structure):
    _fields_ = [("name", ctypes.c_char * GPIO_MAX_NAME_SIZE),
                ("label", ctypes.c_char * GPIO_MAX_NAME_SIZE),
                ("lines", ctypes.c_uint32)]


class _LineInfo(ctypes.Structure):
    _fields_ = [
        ("name", ctypes.c_char * GPIO_MAX_NAME_SIZE),
        ("consumer", ctypes.c_char * GPIO_MAX_NAME_SIZE),
        ("offset", ctypes.c_uint32),
        ("num_attrs", ctypes.c_uint32),
        ("flags", ctypes.c_uint64),
        ("attrs", _LineAttribute * GPIO_V2_LINE_NUM_ATTRS_MAX),
        ("padding", ctypes.c_uint32 * 4),
    ]


GPIO_GET_CHIPINFO_IOCTL = _ioc(_IOC_READ, 0x01, ctypes.sizeof(_ChipInfo))
GPIO_V2_GET_LINEINFO_IOCTL = _ioc(_IOC_READ | _IOC_WRITE, 0x05,
                                  ctypes.sizeof(_LineInfo))
GPIO_V2_GET_LINE_IOCTL = _ioc(_IOC_READ | _IOC_WRITE, 0x07,
                              ctypes.sizeof(_LineRequest))
GPIO_V2_LINE_SET_VALUES_IOCTL = _ioc(_IOC_READ | _IOC_WRITE, 0x0F,
                                     ctypes.sizeof(_LineValues))

# The sizes and request numbers the kernel headers produce on a 64-bit build.
# Every field in the v2 ABI is fixed width with explicit padding, so these do
# not vary between the x86-64 machine this was written on and the Jetson's
# aarch64. A mismatch means a structure above drifted from the ABI, and the
# right moment to find that out is at import on a laptop.
ABI_SIZES = {
    "gpio_v2_line_values": (_LineValues, 16),
    "gpio_v2_line_attribute": (_LineAttribute, 16),
    "gpio_v2_line_config_attribute": (_LineConfigAttribute, 24),
    "gpio_v2_line_config": (_LineConfig, 272),
    "gpio_v2_line_request": (_LineRequest, 592),
    "gpiochip_info": (_ChipInfo, 68),
    "gpio_v2_line_info": (_LineInfo, 256),
}

ABI_REQUESTS = {
    "GPIO_GET_CHIPINFO_IOCTL": (GPIO_GET_CHIPINFO_IOCTL, 0x8044B401),
    "GPIO_V2_GET_LINEINFO_IOCTL": (GPIO_V2_GET_LINEINFO_IOCTL, 0xC100B405),
    "GPIO_V2_GET_LINE_IOCTL": (GPIO_V2_GET_LINE_IOCTL, 0xC250B407),
    "GPIO_V2_LINE_SET_VALUES_IOCTL": (GPIO_V2_LINE_SET_VALUES_IOCTL, 0xC010B40F),
}

# Sizes alone do not pin a layout down. Dropping event_buffer_size from the
# request leaves sizeof at 592, because the four bytes are reclaimed by the
# trailing alignment padding the uint64 in config forces. Every field after
# the hole then shifts, the kernel reads the line number out of the wrong
# bytes, and nothing about it is visible from a size. Offsets are what
# actually catch that, so they are checked too.
ABI_OFFSETS = {
    "gpio_v2_line_values": (_LineValues, {"bits": 0, "mask": 8}),
    "gpio_v2_line_config_attribute": (_LineConfigAttribute,
                                      {"attr": 0, "mask": 16}),
    "gpio_v2_line_config": (_LineConfig,
                            {"flags": 0, "num_attrs": 8, "attrs": 32}),
    "gpio_v2_line_request": (_LineRequest,
                             {"offsets": 0, "consumer": 256, "config": 288,
                              "num_lines": 560, "event_buffer_size": 564,
                              "fd": 588}),
    "gpiochip_info": (_ChipInfo, {"name": 0, "label": 32, "lines": 64}),
    "gpio_v2_line_info": (_LineInfo, {"name": 0, "consumer": 32, "offset": 64,
                                      "num_attrs": 68, "flags": 72}),
}


def abi_mismatches():
    """Everything about this layout that disagrees with the kernel ABI."""
    wrong = []
    for name, (struct, expected) in sorted(ABI_SIZES.items()):
        actual = ctypes.sizeof(struct)
        if actual != expected:
            wrong.append("%s is %d bytes, ABI says %d" % (name, actual, expected))
    for name, (struct, offsets) in sorted(ABI_OFFSETS.items()):
        for field, expected in sorted(offsets.items()):
            attribute = getattr(struct, field, None)
            if attribute is None:
                wrong.append("%s has no field %s" % (name, field))
            elif attribute.offset != expected:
                wrong.append("%s.%s is at %d, ABI says %d"
                             % (name, field, attribute.offset, expected))
    for name, (actual, expected) in sorted(ABI_REQUESTS.items()):
        if actual != expected:
            wrong.append("%s is %#x, ABI says %#x" % (name, actual, expected))
    return wrong


def chip_paths(root=DEV_ROOT):
    """Every GPIO character device present, in name order."""
    return sorted(Path(root).glob(CHIP_GLOB))


def chip_info(path):
    """The chip's kernel name, board label and line count."""
    info = _ChipInfo()
    fd = os.open(str(path), os.O_RDONLY)
    try:
        fcntl.ioctl(fd, GPIO_GET_CHIPINFO_IOCTL, info, True)
    finally:
        os.close(fd)
    return {"name": info.name.decode("utf-8", "replace"),
            "label": info.label.decode("utf-8", "replace"),
            "lines": int(info.lines)}


def line_info(path, offset):
    """One line's name, current consumer and direction.

    The consumer field is what makes a pin safe to claim: a line already held
    by a driver reports that driver's name, and requesting it would fail at the
    worst possible moment rather than during bring-up.
    """
    info = _LineInfo()
    info.offset = int(offset)
    fd = os.open(str(path), os.O_RDONLY)
    try:
        fcntl.ioctl(fd, GPIO_V2_GET_LINEINFO_IOCTL, info, True)
    finally:
        os.close(fd)
    return {
        "offset": int(info.offset),
        "name": info.name.decode("utf-8", "replace"),
        "consumer": info.consumer.decode("utf-8", "replace"),
        "is_output": bool(int(info.flags) & GPIO_V2_LINE_FLAG_OUTPUT),
        "is_input": bool(int(info.flags) & GPIO_V2_LINE_FLAG_INPUT),
    }


class OutputLine:
    """One GPIO line held as an output for the lifetime of a run.

    The line is claimed once and kept. Requesting it per call would put an
    open, an ioctl and a close inside every measured window, and would make the
    line briefly unowned between calls, which is exactly when another process
    could take it.
    """

    def __init__(self, chip, line, consumer="masenergy", initial=0):
        self.chip = str(chip)
        self.line = int(line)
        self._fd = None

        mismatches = abi_mismatches()
        if mismatches:
            raise GpioError(
                "Refusing to drive a line through a mismatched ABI: %s"
                % "; ".join(mismatches))

        request = _LineRequest()
        request.offsets[0] = self.line
        request.num_lines = 1
        request.consumer = consumer.encode("utf-8")[:GPIO_MAX_NAME_SIZE - 1]
        request.config.flags = GPIO_V2_LINE_FLAG_OUTPUT
        request.config.num_attrs = 0

        try:
            chip_fd = os.open(self.chip, os.O_RDONLY)
        except OSError as exc:
            raise GpioError("Cannot open %s: %s" % (self.chip, exc))
        try:
            fcntl.ioctl(chip_fd, GPIO_V2_GET_LINE_IOCTL, request, True)
        except OSError as exc:
            raise GpioError(
                "Cannot claim line %d on %s: %s. Check the line is not already "
                "held by a driver, and that this user can open the chip."
                % (self.line, self.chip, exc))
        finally:
            os.close(chip_fd)

        if request.fd < 0:
            raise GpioError("Kernel returned no descriptor for line %d on %s"
                            % (self.line, self.chip))
        self._fd = int(request.fd)

        self._values = _LineValues()
        self._values.mask = 1
        self._values.bits = 0
        self.set(initial)

    def set(self, value):
        """Drive the line. One ioctl on an open descriptor, nothing allocated."""
        if self._fd is None:
            raise GpioError("Line %d on %s is closed" % (self.line, self.chip))
        self._values.bits = 1 if value else 0
        fcntl.ioctl(self._fd, GPIO_V2_LINE_SET_VALUES_IOCTL, self._values, True)

    def high(self):
        self.set(1)

    def low(self):
        self.set(0)

    def close(self):
        """Drive low, then release.

        Low first because the sampler reads a held-high line as a call still in
        flight. A process that exits with the line high leaves an unterminated
        pulse, which is recoverable; one that leaves it high forever is not.
        """
        if self._fd is None:
            return
        try:
            self.set(0)
        except OSError:
            pass
        os.close(self._fd)
        self._fd = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False
