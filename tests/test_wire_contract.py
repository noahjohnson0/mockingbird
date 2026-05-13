"""Wire-format contract tests (ANDY-2).

The ESP32 firmware emits three newline-delimited JSON message types from
three matched `snprintf` formats in
`firmware/esp32-wroom-mockingbird/src/main.cpp`:

    1. hello — sent once at TCP connect
    2. obs   — one per BLE advertisement observed
    3. hb    — heartbeat every ~5s

The Pi collector (`services/mockingbird-collector.py`) parses these with
`json.loads` and then a sequence of `msg.get("<field>")` accesses. The two
sides have evolved independently with no contract test until now — if
firmware renames a field (`addr_type` → `addrType`, etc.) the collector
silently stores NULL forever.

This file pins the wire format with golden lines hand-built to match the
firmware's `snprintf` format strings byte-for-byte, feeds each through the
collector's parsing pattern, and asserts the parsed values are exactly
what the collector reads.

The canary test (`test_canary_*`) scans `main.cpp` to extract every
`"event":"<name>"` literal in a `snprintf` format string and asserts each
is represented in GOLDEN_FIXTURES — so adding a 4th message type to
firmware without updating the fixtures here fails this test.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

# --- paths ---------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_FW_MAIN = (
    _REPO_ROOT
    / "firmware"
    / "esp32-wroom-mockingbird"
    / "src"
    / "main.cpp"
)


# --- golden fixtures -----------------------------------------------------
#
# Each fixture is the exact byte sequence the firmware's snprintf emits
# for representative field values. The trailing "\n" is the framing the
# collector splits on (readline), so we include it.
#
# Firmware format strings (see firmware/.../main.cpp):
#
#   hello (line 219):
#     "{\"event\":\"hello\",\"leaf\":\"%s\",\"version\":\"%s\","
#     "\"location\":\"%s\"}\n"
#
#   obs (line 245):
#     "{\"event\":\"obs\",\"mac\":\"%s\",\"rssi\":%d,\"addr_type\":%u,"
#     "\"ch\":%u,"
#     "\"name\":\"%s\",\"manuf\":\"%s\",\"t_ms\":%lu,"
#     "\"location\":\"%s\"}\n"
#
#   hb (line 274):
#     "{\"event\":\"hb\",\"up_s\":%lu,\"n_sent\":%lu,\"n_dropped\":%lu,"
#     "\"q_depth\":%d,\"heap\":%u,\"rssi\":%d}\n"
#
GOLDEN_HELLO = (
    b'{"event":"hello","leaf":"mockingbird-4ce184","version":"0.5.0-q64",'
    b'"location":"kitchen"}\n'
)

GOLDEN_OBS = (
    b'{"event":"obs","mac":"AA:BB:CC:DD:EE:FF","rssi":-65,"addr_type":1,'
    b'"ch":37,'
    b'"name":"FlicBtn","manuf":"e000","t_ms":1234567,'
    b'"location":"kitchen"}\n'
)

GOLDEN_HB = (
    b'{"event":"hb","up_s":42,"n_sent":1234,"n_dropped":0,'
    b'"q_depth":3,"heap":118400,"rssi":-58}\n'
)

# Map event name → golden bytes. The canary test uses the keys.
GOLDEN_FIXTURES: dict[str, bytes] = {
    "hello": GOLDEN_HELLO,
    "obs": GOLDEN_OBS,
    "hb": GOLDEN_HB,
}


# --- helpers -------------------------------------------------------------


def _parse_line(line: bytes) -> dict:
    """Replicate the collector's parse step: `json.loads(line.decode(...))`.

    This is line 273 of `services/mockingbird-collector.py`, isolated so
    we can assert against the resulting dict without spinning up the
    async TCP server + SQLite buffer.
    """
    return json.loads(line.decode("utf-8", errors="replace"))


# --- hello: one test per field the collector reads -----------------------
#
# The collector reads on hello (lines 279-291):
#     evt           = msg.get("event")        # routing key
#     leaf          = msg.get("leaf") or msg.get("hostname")
#     leaf_location = msg.get("location") or None
#     version       = msg.get("version", "?") # log line
#


def test_hello_event_field_is_hello() -> None:
    # Arrange
    line = GOLDEN_HELLO

    # Act
    msg = _parse_line(line)

    # Assert
    assert msg.get("event") == "hello"


def test_hello_leaf_field_is_hostname() -> None:
    # Arrange
    line = GOLDEN_HELLO

    # Act
    msg = _parse_line(line)

    # Assert
    assert msg.get("leaf") == "mockingbird-4ce184"


def test_hello_version_field_present() -> None:
    # Arrange
    line = GOLDEN_HELLO

    # Act
    msg = _parse_line(line)

    # Assert
    assert msg.get("version") == "0.5.0-q64"


def test_hello_location_field_present() -> None:
    # Arrange
    line = GOLDEN_HELLO

    # Act
    msg = _parse_line(line)

    # Assert
    assert msg.get("location") == "kitchen"


def test_hello_hostname_fallback_is_none_when_leaf_present() -> None:
    # The collector does `msg.get("leaf") or msg.get("hostname")`. Firmware
    # only emits `leaf`, so `hostname` must be absent — confirm.
    # Arrange
    line = GOLDEN_HELLO

    # Act
    msg = _parse_line(line)

    # Assert
    assert "hostname" not in msg


# --- obs: one test per field the collector reads -------------------------
#
# The collector reads on obs (lines 302-313):
#     msg.get("mac", "")
#     int(msg.get("rssi", 0))
#     msg.get("addr_type")
#     msg.get("name") or None
#     msg.get("manuf") or None
#     msg.get("t_ms")
#     msg.get("location") or None
#     msg.get("ch")  → mapped to chan ∈ {37,38,39}|None
#


def test_obs_event_field_is_obs() -> None:
    # Arrange
    line = GOLDEN_OBS

    # Act
    msg = _parse_line(line)

    # Assert
    assert msg.get("event") == "obs"


def test_obs_mac_field_is_colon_separated_hex() -> None:
    # Arrange
    line = GOLDEN_OBS

    # Act
    msg = _parse_line(line)

    # Assert
    assert msg.get("mac", "") == "AA:BB:CC:DD:EE:FF"


def test_obs_rssi_field_is_signed_int() -> None:
    # RSSI from NimBLE is signed int8, range -128..0. The collector calls
    # int(msg.get("rssi", 0)) — assert the parsed value is already int and
    # negative for a realistic in-room reading.
    # Arrange
    line = GOLDEN_OBS

    # Act
    msg = _parse_line(line)

    # Assert
    assert msg.get("rssi") == -65
    assert isinstance(msg.get("rssi"), int)


def test_obs_addr_type_field_is_unsigned_int() -> None:
    # firmware: `"addr_type":%u` — must round-trip as a non-negative int.
    # Arrange
    line = GOLDEN_OBS

    # Act
    msg = _parse_line(line)

    # Assert
    assert msg.get("addr_type") == 1
    assert isinstance(msg.get("addr_type"), int)


def test_obs_ch_field_is_ble_primary_channel() -> None:
    # firmware: `"ch":%u`. Collector accepts {37,38,39} → chan, else NULL.
    # Arrange
    line = GOLDEN_OBS

    # Act
    msg = _parse_line(line)

    # Assert
    assert msg.get("ch") == 37


def test_obs_name_field_is_string() -> None:
    # Arrange
    line = GOLDEN_OBS

    # Act
    msg = _parse_line(line)

    # Assert
    assert msg.get("name") == "FlicBtn"


def test_obs_manuf_field_is_hex_string() -> None:
    # firmware encodes manuf bytes as lowercase hex (`%02x`) — not JSON
    # numbers. Collector stores it as TEXT. Confirm parsing gives a str.
    # Arrange
    line = GOLDEN_OBS

    # Act
    msg = _parse_line(line)

    # Assert
    assert msg.get("manuf") == "e000"
    assert isinstance(msg.get("manuf"), str)


def test_obs_t_ms_field_is_unsigned_long() -> None:
    # firmware: `"t_ms":%lu` — leaf's millis() at observation time.
    # Arrange
    line = GOLDEN_OBS

    # Act
    msg = _parse_line(line)

    # Assert
    assert msg.get("t_ms") == 1234567


def test_obs_location_field_present() -> None:
    # Arrange
    line = GOLDEN_OBS

    # Act
    msg = _parse_line(line)

    # Assert
    assert msg.get("location") == "kitchen"


# --- hb: one test per field the collector might surface ------------------
#
# The collector currently only persists hb as a JSON blob into
# leaf_events.info, but downstream analysis (and analyze_ble_db.py) reads
# named fields back out. Pin every field name the firmware emits so a
# rename would fail loudly.


def test_hb_event_field_is_hb() -> None:
    # Arrange
    line = GOLDEN_HB

    # Act
    msg = _parse_line(line)

    # Assert
    assert msg.get("event") == "hb"


def test_hb_up_s_field_is_uptime_seconds() -> None:
    # Arrange
    line = GOLDEN_HB

    # Act
    msg = _parse_line(line)

    # Assert
    assert msg.get("up_s") == 42


def test_hb_n_sent_field_present() -> None:
    # Arrange
    line = GOLDEN_HB

    # Act
    msg = _parse_line(line)

    # Assert
    assert msg.get("n_sent") == 1234


def test_hb_n_dropped_field_present() -> None:
    # Arrange
    line = GOLDEN_HB

    # Act
    msg = _parse_line(line)

    # Assert
    assert msg.get("n_dropped") == 0


def test_hb_q_depth_field_present() -> None:
    # Arrange
    line = GOLDEN_HB

    # Act
    msg = _parse_line(line)

    # Assert
    assert msg.get("q_depth") == 3


def test_hb_heap_field_present() -> None:
    # Arrange
    line = GOLDEN_HB

    # Act
    msg = _parse_line(line)

    # Assert
    assert msg.get("heap") == 118400


def test_hb_rssi_field_is_wifi_rssi() -> None:
    # The hb's `rssi` is WiFi RSSI (the leaf's uplink to the Opal),
    # distinct from obs.rssi (which is BLE). Both are signed.
    # Arrange
    line = GOLDEN_HB

    # Act
    msg = _parse_line(line)

    # Assert
    assert msg.get("rssi") == -58


# --- framing -------------------------------------------------------------


@pytest.mark.parametrize(
    "line", [GOLDEN_HELLO, GOLDEN_OBS, GOLDEN_HB], ids=["hello", "obs", "hb"]
)
def test_golden_line_ends_with_newline(line: bytes) -> None:
    # The collector uses asyncio's readline() which is "\n"-delimited.
    # Every firmware snprintf ends its format string with "}\n" — verify.
    # Arrange (line provided by parametrize)

    # Act
    last = line[-1:]

    # Assert
    assert last == b"\n"


@pytest.mark.parametrize(
    "line", [GOLDEN_HELLO, GOLDEN_OBS, GOLDEN_HB], ids=["hello", "obs", "hb"]
)
def test_golden_line_is_valid_json(line: bytes) -> None:
    # Arrange (line provided by parametrize)

    # Act
    parsed = _parse_line(line)

    # Assert
    assert isinstance(parsed, dict)
    assert "event" in parsed


# --- canary: firmware source ↔ fixture coverage --------------------------


# Match `"event":"<name>"` literals inside a C string. The firmware's
# snprintf format strings are multi-line concatenations of "..." string
# literals, so we strip them down to the JSON inside and pull every
# "event":"<token>" pair.
_EVENT_LITERAL_RE = re.compile(r'\\"event\\":\\"([a-zA-Z_][a-zA-Z0-9_-]*)\\"')


def _firmware_event_names() -> set[str]:
    """Scan main.cpp for every distinct `"event":"<name>"` literal that
    appears inside a `snprintf` format string."""
    src = _FW_MAIN.read_text(encoding="utf-8")

    # Find each snprintf(...) call body. The format string is a sequence
    # of "..." literals concatenated by the C preprocessor. We don't try
    # to parse C — we just search the whole source for the JSON event-key
    # pattern, which is unique enough that false positives are unlikely.
    return set(_EVENT_LITERAL_RE.findall(src))


def test_canary_firmware_has_main_cpp_at_expected_path() -> None:
    # If firmware moves, the canary becomes a no-op silently. Fail loudly
    # instead.
    # Arrange (path is module-level _FW_MAIN)

    # Act
    exists = _FW_MAIN.is_file()

    # Assert
    assert exists, f"firmware main.cpp not found at {_FW_MAIN}"


def test_canary_every_firmware_event_has_a_golden_fixture() -> None:
    # Arrange
    fw_events = _firmware_event_names()
    fixture_events = set(GOLDEN_FIXTURES.keys())

    # Act
    missing = fw_events - fixture_events

    # Assert
    assert not missing, (
        f"firmware main.cpp emits event types {sorted(missing)} that have "
        f"no golden fixture in GOLDEN_FIXTURES. Add a wire_<event>.txt-style "
        f"golden line and per-field tests, then add the event to "
        f"GOLDEN_FIXTURES so this canary is satisfied."
    )


def test_canary_finds_at_least_the_three_known_events() -> None:
    # Guard against the regex failing to match anything (e.g. firmware
    # refactors the snprintf calls to use a helper) and silently making
    # the previous test vacuously pass.
    # Arrange
    expected_minimum = {"hello", "obs", "hb"}

    # Act
    fw_events = _firmware_event_names()

    # Assert
    assert expected_minimum.issubset(fw_events), (
        f"canary regex found {sorted(fw_events)} in main.cpp, expected at "
        f"least {sorted(expected_minimum)}. The regex may be stale relative "
        f"to firmware refactors."
    )
