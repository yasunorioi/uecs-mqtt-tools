"""ccm_simulator の pytest — 各 value generator、XML build、scheduler の unit test."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "ccm_simulator.py"
_spec = importlib.util.spec_from_file_location("ccm_simulator", _MODULE_PATH)
assert _spec and _spec.loader
sim = importlib.util.module_from_spec(_spec)
sys.modules["ccm_simulator"] = sim
_spec.loader.exec_module(sim)


# ── value generators ─────────────────────────

def test_constant():
    g = sim.Constant(value=42.0)
    assert g.value_at(0) == 42.0
    assert g.value_at(9999) == 42.0


def test_ramp_start_and_end_and_middle():
    g = sim.Ramp(start=10, end=30, duration_sec=100)
    assert g.value_at(0) == 10.0
    assert g.value_at(100) == 30.0
    assert g.value_at(200) == 30.0     # clamp
    assert g.value_at(50) == 20.0      # 中央


def test_ramp_negative_clamped_to_start():
    g = sim.Ramp(start=5, end=10, duration_sec=60)
    assert g.value_at(-10) == 5.0


def test_sine_zero_period_returns_center():
    g = sim.Sine(center=25, amplitude=3, period_sec=0)
    assert g.value_at(0) == 25.0


def test_sine_shape():
    g = sim.Sine(center=0, amplitude=1, period_sec=4)
    assert abs(g.value_at(0)) < 1e-6           # sin(0)
    assert abs(g.value_at(1) - 1.0) < 1e-6     # sin(π/2)
    assert abs(g.value_at(2)) < 1e-6           # sin(π)
    assert abs(g.value_at(3) - (-1.0)) < 1e-6  # sin(3π/2)


def test_jitter_reproducible_with_seed():
    g1 = sim.Jitter(base=10, sigma=1, seed=7)
    g2 = sim.Jitter(base=10, sigma=1, seed=7)
    seq1 = [g1.value_at(t) for t in range(5)]
    seq2 = [g2.value_at(t) for t in range(5)]
    assert seq1 == seq2


def test_build_generator_dispatch():
    assert isinstance(sim.build_generator({"kind": "constant", "value": 1}), sim.Constant)
    assert isinstance(sim.build_generator({
        "kind": "ramp", "start": 0, "end": 1, "duration_sec": 1
    }), sim.Ramp)
    assert isinstance(sim.build_generator({
        "kind": "sine", "center": 0, "amplitude": 1, "period_sec": 1
    }), sim.Sine)
    assert isinstance(sim.build_generator({
        "kind": "jitter", "base": 0, "sigma": 1
    }), sim.Jitter)


def test_build_generator_unknown_raises():
    try:
        sim.build_generator({"kind": "chaos", "chaos_level": 42})
    except ValueError as e:
        assert "chaos" in str(e)
    else:
        raise AssertionError("expected ValueError")


# ── DATA XML build ───────────────────────────

def test_build_data_xml_has_all_attrs_and_value():
    c = sim.CCM(type="InAirTemp", room=1, region=11, order=2, priority=15,
                gen=sim.Constant(22.5))
    xml = c.build_data_xml(t=0)
    assert '<DATA type="InAirTemp.cMC"' in xml
    assert 'room="1"' in xml
    assert 'region="11"' in xml
    assert 'order="2"' in xml
    assert 'priority="15"' in xml
    assert '>22.50<' in xml
    assert xml.endswith("</DATA>")


def test_build_packet_wraps_datas_in_uecs_root():
    ccm = sim.CCM(type="X", room=1, region=1, gen=sim.Constant(1))
    p = sim.build_packet([ccm.build_data_xml(0)])
    text = p.decode("utf-8")
    assert text.startswith('<?xml version="1.0"?>')
    assert "<UECS" in text
    assert "</UECS>" in text
    assert "<DATA" in text


# ── Node emit_packets: 1-DATA vs multi-DATA モード ──

def _mk_node(num_ccms=3):
    ccms = [
        sim.CCM(type=f"T{i}", room=1, region=11, gen=sim.Constant(i))
        for i in range(num_ccms)
    ]
    return sim.Node(name="n", interval_sec=1, ccms=ccms)


def test_emit_default_is_1_data_per_packet():
    node = _mk_node(num_ccms=3)
    packets = node.emit_packets(elapsed=0, multi_data_per_packet=False)
    assert len(packets) == 3
    # 各 packet に 1 個ずつ
    for p in packets:
        assert p.decode("utf-8").count("<DATA") == 1


def test_emit_multi_data_mode_packs_all_in_one():
    node = _mk_node(num_ccms=3)
    packets = node.emit_packets(elapsed=0, multi_data_per_packet=True)
    assert len(packets) == 1
    assert packets[0].decode("utf-8").count("<DATA") == 3


# ── scheduler (Node.due / advance) ───────────

def test_node_due_after_interval():
    node = _mk_node()
    assert node.due(0.0) is True
    node.advance()
    assert node.due(0.5) is False
    assert node.due(1.0) is True


# ── load_scenario 統合 ───────────────────────

def test_load_scenario_full(tmp_path):
    yaml_text = """
target:
  multicast: 127.0.0.1
  port: 20000
nodes:
  - name: sensor_a
    interval_sec: 5
    ccms:
      - type: InAirTemp
        room: 1
        region: 11
        order: 1
        value: {kind: constant, value: 22.5}
      - type: InAirHumid
        room: 1
        region: 11
        order: 1
        value: {kind: sine, center: 80, amplitude: 5, period_sec: 60}
"""
    p = tmp_path / "s.yaml"
    p.write_text(yaml_text)
    nodes, target = sim.load_scenario(p)
    assert target["multicast"] == "127.0.0.1"
    assert target["port"] == 20000
    assert len(nodes) == 1
    n = nodes[0]
    assert n.name == "sensor_a"
    assert n.interval_sec == 5.0
    assert len(n.ccms) == 2
    assert isinstance(n.ccms[0].gen, sim.Constant)
    assert isinstance(n.ccms[1].gen, sim.Sine)


# ── run() with sender injection (no real UDP) ──

def test_run_with_sender_hook(tmp_path):
    ccms = [sim.CCM(type="InAirTemp", room=1, region=11,
                    gen=sim.Constant(25.0))]
    nodes = [sim.Node(name="n1", interval_sec=1.0, ccms=ccms)]

    sent: list[bytes] = []
    stats = sim.run(
        nodes, "127.0.0.1", 16520,
        duration=0.2,   # 少なくとも 1 回発火
        multi_data_per_packet=False,
        dry_run=False,
        tick_sec=0.05,
        sender=lambda p: sent.append(p),
    )
    assert stats["packets"] >= 1
    assert stats["nodes_fired"]["n1"] >= 1
    # 中身が DATA を持ってる
    assert b"<DATA" in sent[0]
