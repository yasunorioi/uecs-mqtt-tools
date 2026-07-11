"""uecs_webui の pytest — state 更新 / grouping / HTML render の純関数部."""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "uecs_webui.py"
_spec = importlib.util.spec_from_file_location("uecs_webui", _MODULE_PATH)
assert _spec and _spec.loader
ui = importlib.util.module_from_spec(_spec)
sys.modules["uecs_webui"] = ui
_spec.loader.exec_module(ui)


# ── State.on_message ────────────────────────

def test_on_message_parses_bridge_payload():
    s = ui.State()
    payload = json.dumps({"value": 22.5, "unit": "C", "ts": 1783780000}).encode()
    s.on_message("agriha/1/sensor/InAirTemp", payload)
    assert s.total_messages == 1
    e = s.topics["agriha/1/sensor/InAirTemp"]
    assert e.value == 22.5
    assert e.unit == "C"
    assert e.ts == 1783780000


def test_on_message_updates_existing_topic():
    s = ui.State()
    s.on_message("t", json.dumps({"value": 1, "ts": 100}).encode())
    s.on_message("t", json.dumps({"value": 2, "ts": 110}).encode())
    assert s.total_messages == 2
    assert s.topics["t"].value == 2
    assert s.topics["t"].ts == 110


def test_on_message_collision_recorded_when_same_ts_different_value():
    """同 ts で違う value が来たら collision に積む。"""
    s = ui.State()
    s.on_message("t", json.dumps({"value": 1, "ts": 100}).encode())
    s.on_message("t", json.dumps({"value": 2, "ts": 100}).encode())
    assert len(s.collisions) == 1
    c = s.collisions[0]
    assert c["topic"] == "t"
    assert c["prev_value"] == 1
    assert c["new_value"] == 2


def test_on_message_no_collision_when_ts_advances():
    s = ui.State()
    s.on_message("t", json.dumps({"value": 1, "ts": 100}).encode())
    s.on_message("t", json.dumps({"value": 2, "ts": 110}).encode())
    assert len(s.collisions) == 0


def test_on_message_malformed_payload_kept_as_raw():
    s = ui.State()
    s.on_message("t", b"not json at all")
    e = s.topics["t"]
    assert e.value is None
    assert e.payload_raw == "not json at all"


# ── group_topics ────────────────────────────

def _mk_entry(topic, value=1, ts=100):
    return ui.TopicEntry(
        topic=topic, value=value, unit="", ts=ts, received_at=ts, payload_raw="",
    )


def test_group_topics_by_scope_and_category():
    entries = [
        _mk_entry("agriha/1/sensor/InAirTemp"),
        _mk_entry("agriha/1/sensor/InAirCO2"),
        _mk_entry("agriha/1/actuator/Alert"),
        _mk_entry("agriha/farm/weather/WAirTemp"),
    ]
    g = ui.group_topics(entries, prefix="agriha")
    assert set(g.keys()) == {"1", "farm"}
    assert set(g["1"].keys()) == {"sensor", "actuator"}
    assert len(g["1"]["sensor"]) == 2
    assert len(g["farm"]["weather"]) == 1


def test_group_topics_prefix_mismatch_falls_into_other():
    entries = [_mk_entry("otherprefix/x/y")]
    g = ui.group_topics(entries, prefix="agriha")
    assert "_other" in g


def test_group_topics_short_path_falls_into_other():
    entries = [_mk_entry("agriha/scope")]
    g = ui.group_topics(entries, prefix="agriha")
    assert "_other" in g


# ── format_age ───────────────────────────────

def test_format_age_tiers():
    # 閾値: <30 fresh, <300 warm, <3600 stale, else dead。ts=0 は unknown
    now = 10_000.0
    assert ui.format_age(now, now - 15)[1] == "fresh"    # 15s
    assert ui.format_age(now, now - 100)[1] == "warm"    # 100s
    assert ui.format_age(now, now - 900)[1] == "stale"   # 900s = 15m
    assert ui.format_age(now, now - 7200)[1] == "dead"   # 2h
    assert ui.format_age(now, 0)[1] == "unknown"


def test_format_age_string_units():
    now = 10_000.0
    assert ui.format_age(now, now - 10)[0].endswith("s")     # 10s
    assert ui.format_age(now, now - 500)[0].endswith("m")    # 500s → 8m
    assert ui.format_age(now, now - 7200)[0].endswith("h")   # 2h


# ── _fmt_value ───────────────────────────────

def test_fmt_value_with_unit():
    assert ui._fmt_value(22.5, "C") == "22.5 C"


def test_fmt_value_none():
    assert ui._fmt_value(None, "C") == "—"


def test_fmt_value_string_passthrough():
    assert ui._fmt_value("running", "") == "running"


# ── render_partial (smoke) ──────────────────

def test_render_partial_empty_state_has_empty_message():
    s = ui.State()
    out = ui.render_partial(s, prefix="agriha")
    assert "no topics received yet" in out
    assert "no collisions" in out


def test_render_partial_shows_topics_and_scope_groups():
    s = ui.State()
    s.on_message("agriha/1/sensor/InAirTemp",
                 json.dumps({"value": 22.5, "unit": "C", "ts": time.time()}).encode())
    s.on_message("agriha/farm/weather/WAirTemp",
                 json.dumps({"value": 15.0, "unit": "C", "ts": time.time()}).encode())
    out = ui.render_partial(s, prefix="agriha")
    assert "scope: 1" in out
    assert "scope: farm" in out
    assert "InAirTemp" in out
    assert "WAirTemp" in out
    assert "22.5 C" in out


def test_render_partial_shows_collision():
    s = ui.State()
    s.on_message("agriha/1/sensor/X",
                 json.dumps({"value": 1, "ts": 100}).encode())
    s.on_message("agriha/1/sensor/X",
                 json.dumps({"value": 2, "ts": 100}).encode())
    out = ui.render_partial(s, prefix="agriha")
    assert "prev" in out
    # 具体的な collision 行が表示される
    assert "agriha/1/sensor/X" in out


# ── render_index (smoke) ────────────────────

def test_render_index_shows_broker_info():
    out = ui.render_index("100.102.95.37", 1883, "agriha", None)
    assert "100.102.95.37" in out
    assert "agriha" in out
    assert "not yet" in out  # subscribed 時刻 未設定
    assert "htmx.org" in out  # HTMX 読み込み
