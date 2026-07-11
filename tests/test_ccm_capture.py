"""ccm_capture の pytest — パーサ / フィルタ / inspect の集計を確認."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "ccm_capture.py"
_spec = importlib.util.spec_from_file_location("ccm_capture", _MODULE_PATH)
assert _spec and _spec.loader
cap = importlib.util.module_from_spec(_spec)
sys.modules["ccm_capture"] = cap
_spec.loader.exec_module(cap)


# ── DATA XML パーサ ───────────────────────────

def test_parse_single_data_all_attrs():
    payload = (
        '<?xml version="1.0"?>'
        '<UECS><DATA type="InAirTemp.cMC" room="1" region="11" order="1" '
        'priority="15">21.3</DATA></UECS>'
    )
    got = cap.parse_data_elements(payload)
    assert len(got) == 1
    e = got[0]
    assert e["type_raw"] == "InAirTemp.cMC"
    assert e["type"] == "InAirTemp"   # suffix strip
    assert e["room"] == 1
    assert e["region"] == 11
    assert e["order"] == 1
    assert e["priority"] == 15
    assert e["value"] == 21.3


def test_parse_multi_data_in_single_packet():
    payload = (
        '<UECS>'
        '<DATA type="InAirTemp" room="1" region="11" order="1">22.5</DATA>'
        '<DATA type="InAirHumid" room="1" region="11" order="1">88</DATA>'
        '</UECS>'
    )
    got = cap.parse_data_elements(payload)
    assert len(got) == 2
    assert [d["type"] for d in got] == ["InAirTemp", "InAirHumid"]


def test_parse_missing_order_becomes_none():
    """order 欠落は None 保持 (send-side 都合で ArsProut は order 落とすことあり)。"""
    payload = '<DATA type="Pulse" room="1" region="12">42</DATA>'
    got = cap.parse_data_elements(payload)
    assert got[0]["order"] is None
    assert got[0]["room"] == 1


def test_parse_string_value_when_not_numeric():
    payload = '<DATA type="cnd" room="1" region="41">running</DATA>'
    got = cap.parse_data_elements(payload)
    assert got[0]["value"] == "running"


def test_parse_zero_data_in_junk():
    got = cap.parse_data_elements("not xml at all")
    assert got == []


def test_parse_attribute_order_variance():
    """UECS 送信側実装で属性順序が違うケースを想定 (region 先など)。"""
    payload = '<DATA region="11" type="InAirCO2" room="1" order="2">450</DATA>'
    got = cap.parse_data_elements(payload)
    assert got[0]["type"] == "InAirCO2"
    assert got[0]["room"] == 1
    assert got[0]["region"] == 11
    assert got[0]["order"] == 2


# ── Filter ─────────────────────────────────────

def test_filter_by_type():
    f = cap.Filter(types=["InAirTemp"])
    assert f.match_data({"type": "InAirTemp", "room": 1, "region": 11})
    assert not f.match_data({"type": "InAirCO2", "room": 1, "region": 11})


def test_filter_by_room_region():
    f = cap.Filter(rooms=[1], regions=[11, 12])
    assert f.match_data({"type": "X", "room": 1, "region": 11})
    assert f.match_data({"type": "X", "room": 1, "region": 12})
    assert not f.match_data({"type": "X", "room": 1, "region": 41})
    assert not f.match_data({"type": "X", "room": 2, "region": 11})


def test_filter_by_ip_at_packet_level():
    f = cap.Filter(ips=["192.168.1.80"])
    assert f.match_packet("192.168.1.80")
    assert not f.match_packet("192.168.1.70")


def test_filter_no_filter_passes_all():
    f = cap.Filter()
    assert not f.has_data_filter
    assert f.match_packet("any.ip")
    assert f.match_data({"type": "X", "room": 99, "region": 99})


# ── build_entry ────────────────────────────────

def test_build_entry_marks_multi_data_warn():
    elements = [
        {"type": "InAirTemp", "room": 1, "region": 11},
        {"type": "InAirHumid", "room": 1, "region": 11},
    ]
    e = cap.build_entry("192.168.1.80", "<raw>", elements, include_raw=False)
    assert e["num_data"] == 2
    assert e["warn"] == "multi_data_in_single_packet"
    assert "raw" not in e


def test_build_entry_single_data_no_warn():
    elements = [{"type": "InAirTemp", "room": 1, "region": 11}]
    e = cap.build_entry("192.168.1.80", "<raw>", elements, include_raw=True)
    assert e["num_data"] == 1
    assert "warn" not in e
    assert e["raw"] == "<raw>"


# ── inspect ────────────────────────────────────

def test_inspect_summarizes_and_flags_multi_data(tmp_path, capsys):
    log = tmp_path / "cap.jsonl"
    entries = [
        {"ts": "2026-07-11T10:00:00+09:00", "src_ip": "192.168.1.70",
         "raw_len": 100, "num_data": 1,
         "elements": [{"type": "InAirTemp", "room": 1, "region": 11}]},
        {"ts": "2026-07-11T10:00:05+09:00", "src_ip": "192.168.1.80",
         "raw_len": 150, "num_data": 2, "warn": "multi_data_in_single_packet",
         "elements": [
             {"type": "InAirTemp", "room": 1, "region": 12},
             {"type": "InAirHumid", "room": 1, "region": 12},
         ]},
        {"ts": "2026-07-11T10:00:10+09:00", "src_ip": "192.168.1.71",
         "raw_len": 90, "num_data": 1,
         "elements": [{"type": "WRainfallAmt", "room": 1, "region": 41}]},
    ]
    with log.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")

    rc = cap.main(["inspect", "--log", str(log)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "packets    : 3" in out
    assert "DATA elems : 4" in out
    assert "multi_data : 1" in out
    assert "192.168.1.70" in out
    assert "InAirTemp" in out


def test_inspect_with_type_filter(tmp_path, capsys):
    log = tmp_path / "cap.jsonl"
    entries = [
        {"ts": "t1", "src_ip": "a", "raw_len": 1, "num_data": 1,
         "elements": [{"type": "InAirTemp", "room": 1, "region": 11}]},
        {"ts": "t2", "src_ip": "b", "raw_len": 1, "num_data": 1,
         "elements": [{"type": "InAirCO2", "room": 1, "region": 11}]},
    ]
    with log.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")

    rc = cap.main(["inspect", "--log", str(log), "--type", "InAirTemp"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "packets    : 1" in out
    assert "InAirTemp" in out
    assert "InAirCO2" not in out
