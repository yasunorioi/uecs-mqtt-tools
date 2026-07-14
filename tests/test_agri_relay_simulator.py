"""test_agri_relay_simulator.py — 純関数部分 (parse/build/handle_packet/emit) の unit test.

socket / signal / real UDP は差し替え可能な interface (recv_sock, sender) で
テスト、実 socket 使わない。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
from agri_relay_simulator import (  # noqa: E402
    Actuator,
    Config,
    build_data_xml,
    build_packet,
    emit_state_packets,
    handle_packet,
    load_config,
    parse_data_elements,
    run,
)


# ══════════════════════════════════════════════
# parse_data_elements
# ══════════════════════════════════════════════

def test_parse_single_rcA() -> None:
    xml = (
        '<?xml version="1.0"?><UECS ver="1.00-E10">'
        '<DATA type="IrrircA" room="1" region="71" priority="15" order="1">1</DATA>'
        '</UECS>'
    )
    out = parse_data_elements(xml)
    assert len(out) == 1
    d = out[0]
    assert d["type"] == "IrrircA"
    assert d["value"] == 1.0
    assert d["room"] == 1 and d["region"] == 71 and d["order"] == 1


def test_parse_multi_data() -> None:
    xml = (
        '<UECS ver="1.00-E10">'
        '<DATA type="RelayrcA" room="1" region="71" priority="15" order="3">1</DATA>'
        '<DATA type="RelayrcA" room="1" region="71" priority="15" order="4">0</DATA>'
        '</UECS>'
    )
    out = parse_data_elements(xml)
    assert len(out) == 2
    assert [d["order"] for d in out] == [3, 4]


def test_parse_suffix_stripped() -> None:
    xml = '<DATA type="Relayopr.cMC" room="1" region="71" order="1">42</DATA>'
    out = parse_data_elements(xml)
    assert out[0]["type"] == "Relayopr"
    assert out[0]["type_raw"] == "Relayopr.cMC"


def test_parse_broken_xml_returns_empty() -> None:
    assert parse_data_elements("<not-xml") == []
    assert parse_data_elements("") == []


def test_parse_non_numeric_value() -> None:
    xml = '<DATA type="AlertrcA" room="1" region="71" order="1">high_temp</DATA>'
    out = parse_data_elements(xml)
    assert out[0]["value"] == "high_temp"


# ══════════════════════════════════════════════
# build_data_xml / build_packet
# ══════════════════════════════════════════════

def test_build_data_xml_integer_value() -> None:
    xml = build_data_xml("Irriopr", 1, 71, 1, 15, 1)
    assert 'type="Irriopr"' in xml
    assert '>1<' in xml
    assert 'room="1"' in xml and 'region="71"' in xml


def test_build_data_xml_float_value() -> None:
    xml = build_data_xml("Irriopr", 1, 71, 1, 15, 3.14159)
    assert '>3.142<' in xml  # 3 decimal places


def test_build_packet_wraps_uecs() -> None:
    xml1 = build_data_xml("Relayopr", 1, 71, 1, 15, 1)
    pkt = build_packet([xml1])
    body = pkt.decode()
    assert body.startswith('<?xml version="1.0"?><UECS ver="1.00-E10">')
    assert body.endswith('</UECS>')


# ══════════════════════════════════════════════
# handle_packet — rcA 受信 → actuator state 更新
# ══════════════════════════════════════════════

def _make_actuators() -> dict[tuple[str, int], Actuator]:
    irri = Actuator(name="irrigation", cmd_type="IrrircA", state_type="Irriopr", order=1, initial=0)
    relay3 = Actuator(name="co2_gen", cmd_type="RelayrcA", state_type="Relayopr", order=3, initial=0)
    return {(a.cmd_type, a.order): a for a in (irri, relay3)}


def test_handle_packet_applies_matching_cmd() -> None:
    actuators = _make_actuators()
    xml = '<DATA type="IrrircA" room="1" region="71" order="1">1</DATA>'
    entries = handle_packet(xml, "192.168.1.10", actuators, 1, 71, now_ts=1000.0)
    assert len(entries) == 1
    assert entries[0]["event"] == "cmd_applied"
    assert entries[0]["actuator"] == "irrigation"
    assert entries[0]["prev"] == 0
    assert entries[0]["new"] == 1.0
    assert actuators[("IrrircA", 1)].current == 1.0


def test_handle_packet_ignores_wrong_room() -> None:
    actuators = _make_actuators()
    xml = '<DATA type="IrrircA" room="2" region="71" order="1">1</DATA>'
    entries = handle_packet(xml, "src", actuators, 1, 71, 0.0)
    assert entries == []
    assert actuators[("IrrircA", 1)].current == 0  # not applied


def test_handle_packet_ignores_wrong_region() -> None:
    actuators = _make_actuators()
    xml = '<DATA type="IrrircA" room="1" region="61" order="1">1</DATA>'
    entries = handle_packet(xml, "src", actuators, 1, 71, 0.0)
    assert entries == []


def test_handle_packet_ignores_wrong_order() -> None:
    """order で区別する actuator (relay ch3 と ch4 が同 cmd_type) — order 違いは unmapped_cmd。"""
    actuators = _make_actuators()
    xml = '<DATA type="RelayrcA" room="1" region="71" order="4">1</DATA>'
    entries = handle_packet(xml, "src", actuators, 1, 71, 0.0)
    # order=4 は registry 未登録 → unmapped_cmd として記録される (silent drop ではない)
    assert len(entries) == 1
    assert entries[0]["event"] == "unmapped_cmd"
    assert actuators[("RelayrcA", 3)].current == 0


def test_handle_packet_same_cmd_type_different_orders() -> None:
    """現地 VenSdWinrcA order=1/2 パターン: 両方受信されるべき (registry bug 回帰防止)。"""
    win1 = Actuator(name="south_lower", cmd_type="VenSdWinrcA", state_type="VenSdWinopr", order=1, initial=0)
    win2 = Actuator(name="south_upper", cmd_type="VenSdWinrcA", state_type="VenSdWinopr", order=2, initial=0)
    actuators = {(a.cmd_type, a.order): a for a in (win1, win2)}
    xml = (
        '<UECS>'
        '<DATA type="VenSdWinrcA" room="1" region="71" order="1">30</DATA>'
        '<DATA type="VenSdWinrcA" room="1" region="71" order="2">30</DATA>'
        '</UECS>'
    )
    entries = handle_packet(xml, "src", actuators, 1, 71, 0.0)
    assert len(entries) == 2
    assert {e["actuator"] for e in entries} == {"south_lower", "south_upper"}
    assert actuators[("VenSdWinrcA", 1)].current == 30.0
    assert actuators[("VenSdWinrcA", 2)].current == 30.0


def test_handle_packet_missing_order_treated_as_1() -> None:
    """UECS 仕様: order 属性欠落は主系統 1 として扱う。"""
    actuators = _make_actuators()
    xml = '<DATA type="IrrircA" room="1" region="71">1</DATA>'  # order 属性なし
    entries = handle_packet(xml, "src", actuators, 1, 71, 0.0)
    assert len(entries) == 1
    assert entries[0]["event"] == "cmd_applied"
    assert actuators[("IrrircA", 1)].current == 1.0


def test_handle_packet_ignores_opr_type() -> None:
    """自分 (or 他 device) の opr 送信は無視 (受信対象は rcA のみ)。"""
    actuators = _make_actuators()
    xml = '<DATA type="Irriopr" room="1" region="71" order="1">1</DATA>'
    entries = handle_packet(xml, "src", actuators, 1, 71, 0.0)
    assert entries == []


def test_handle_packet_unmapped_cmd_logged() -> None:
    """config で扱ってない rcA は unmapped 扱いで log には残す。"""
    actuators = _make_actuators()
    xml = '<DATA type="ValvercA" room="1" region="71" order="1">1</DATA>'
    entries = handle_packet(xml, "src", actuators, 1, 71, 0.0)
    assert len(entries) == 1
    assert entries[0]["event"] == "unmapped_cmd"


def test_handle_packet_multi_data() -> None:
    actuators = _make_actuators()
    xml = (
        '<UECS>'
        '<DATA type="IrrircA" room="1" region="71" order="1">1</DATA>'
        '<DATA type="RelayrcA" room="1" region="71" order="3">1</DATA>'
        '</UECS>'
    )
    entries = handle_packet(xml, "src", actuators, 1, 71, 0.0)
    assert len(entries) == 2
    assert {e["actuator"] for e in entries} == {"irrigation", "co2_gen"}


# ══════════════════════════════════════════════
# emit_state_packets
# ══════════════════════════════════════════════

def test_emit_state_packets_all_actuators() -> None:
    cfg = Config(
        device_name="test", room=1, region=71,
        multicast="224.0.0.1", port=16520, bind_ip="0.0.0.0",
        actuators=[
            Actuator(name="a", cmd_type="XrcA", state_type="Xopr", order=1, initial=5),
            Actuator(name="b", cmd_type="YrcA", state_type="Yopr", order=2, initial=10),
        ],
        state_emit_interval_sec=10.0, log_path=None,
    )
    by_name = {a.name: a for a in cfg.actuators}
    sent: list[tuple[bytes, str, int]] = []
    n = emit_state_packets(cfg, by_name, lambda p, h, port: sent.append((p, h, port)))
    assert n == 2
    assert len(sent) == 2
    xml0 = sent[0][0].decode()
    assert 'type="Xopr"' in xml0 and '>5<' in xml0


# ══════════════════════════════════════════════
# load_config
# ══════════════════════════════════════════════

def test_load_config_full(tmp_path: Path) -> None:
    p = tmp_path / "cfg.yaml"
    p.write_text(yaml.safe_dump({
        "device": {"name": "sim1", "room": 1, "region": 71},
        "target": {"multicast": "224.0.0.1", "port": 16520},
        "actuators": [
            {"name": "irr", "cmd_type": "IrrircA", "state_type": "Irriopr", "order": 1, "initial": 0},
        ],
        "state_emit_interval_sec": 5.0,
        "log_path": "/tmp/log.jsonl",
    }))
    cfg = load_config(p)
    assert cfg.device_name == "sim1"
    assert cfg.room == 1 and cfg.region == 71
    assert len(cfg.actuators) == 1
    assert cfg.actuators[0].cmd_type == "IrrircA"
    assert cfg.state_emit_interval_sec == 5.0
    assert cfg.log_path == "/tmp/log.jsonl"


def test_load_config_defaults(tmp_path: Path) -> None:
    p = tmp_path / "cfg.yaml"
    p.write_text(yaml.safe_dump({"actuators": []}))
    cfg = load_config(p)
    assert cfg.room == 1 and cfg.region == 71
    assert cfg.multicast == "224.0.0.1" and cfg.port == 16520
    assert cfg.state_emit_interval_sec == 10.0
    assert cfg.log_path is None


# ══════════════════════════════════════════════
# run() — mock recv_sock で 1 tick
# ══════════════════════════════════════════════

class MockSocket:
    """recv_sock 差し替え用 mock、queue から順に recvfrom を返す。"""
    def __init__(self, payloads: list[tuple[bytes, tuple[str, int]]]) -> None:
        self.payloads = list(payloads)

    def recvfrom(self, bufsize: int) -> tuple[bytes, tuple[str, int]]:
        if not self.payloads:
            import socket as sock_mod
            raise sock_mod.timeout("mock timeout")
        return self.payloads.pop(0)

    def close(self) -> None:
        pass


def test_run_processes_incoming_cmd_and_emits_state(tmp_path: Path) -> None:
    cfg = Config(
        device_name="test", room=1, region=71,
        multicast="224.0.0.1", port=16520, bind_ip="0.0.0.0",
        actuators=[
            Actuator(name="irr", cmd_type="IrrircA", state_type="Irriopr", order=1, initial=0),
        ],
        state_emit_interval_sec=100.0,  # 大きくして tick 内 emit させない (initial のみ)
        log_path=str(tmp_path / "log.jsonl"),
    )
    incoming = (
        b'<?xml version="1.0"?><UECS><DATA type="IrrircA" room="1" region="71" order="1">1</DATA></UECS>',
        ("192.168.1.10", 12345),
    )
    mock_sock = MockSocket([incoming])
    sent: list[tuple[bytes, str, int]] = []

    # 時刻進行を制御
    times = iter([1000.0, 1000.1, 1000.2, 1000.3, 1000.4, 1000.5])
    def now_fn() -> float:
        return next(times, 1001.0)

    stats = run(
        cfg=cfg,
        duration=0.3,
        recv_sock=mock_sock,
        sender=lambda p, h, port: sent.append((p, h, port)),
        tick_sec=0.05,
        now_provider=now_fn,
    )

    # command 1 件受理、initial state emit 1 件
    assert stats["cmds_received"] == 1
    assert stats["state_packets_sent"] >= 1
    assert cfg.actuators[0].current == 1.0

    # log jsonl 確認
    log_lines = (tmp_path / "log.jsonl").read_text().strip().split("\n")
    entries = [json.loads(ln) for ln in log_lines if ln]
    assert any(e["event"] == "cmd_applied" and e["actuator"] == "irr" for e in entries)


def test_run_dry_run_no_sender_call(tmp_path: Path, capsys) -> None:
    """dry_run=True で sender をデフォルトの stdout 出力に差し替え、実 socket 使わない。"""
    cfg = Config(
        device_name="test", room=1, region=71,
        multicast="224.0.0.1", port=16520, bind_ip="0.0.0.0",
        actuators=[
            Actuator(name="a", cmd_type="XrcA", state_type="Xopr", order=1, initial=0),
        ],
        state_emit_interval_sec=100.0,
        log_path=None,
    )
    mock_sock = MockSocket([])
    times = iter([1000.0, 1000.1, 1000.2])
    stats = run(
        cfg=cfg, duration=0.15, dry_run=True,
        recv_sock=mock_sock, sender=None,
        tick_sec=0.05,
        now_provider=lambda: next(times, 1001.0),
    )
    captured = capsys.readouterr()
    assert "[dry-run send]" in captured.out
    assert stats["state_packets_sent"] >= 1
