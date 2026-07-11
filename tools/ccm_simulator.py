"""ccm_simulator.py — 合成 UECS-CCM UDP パケット送出ツール.

実 CCM 機器が無い環境で bridge / capture を叩くためのシミュレータ。
YAML シナリオで node ごとに `<DATA>` 要素と value generator (constant / ramp / sine /
jitter) を定義し、指定 interval で multicast (or unicast) 送出する。

現地観測で判明した ArsProut 実装の癖に合わせ、default は **1 packet = 1 DATA**。
`--multi-data-per-packet` で複数 DATA を 1 packet に詰めるモードも用意 (bridge の
collision 検出テスト用)。

依存: 標準ライブラリ + pyyaml。

Usage:
  # loopback で bridge/capture を叩く
  python3 ccm_simulator.py --config scenario.yaml \\
      --target 127.0.0.1 --duration 60

  # dry-run (実送出せず内容だけ確認)
  python3 ccm_simulator.py --config scenario.yaml --dry-run --duration 5
"""

from __future__ import annotations

import argparse
import math
import random
import signal
import socket
import struct
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

import yaml


_JST = ZoneInfo("Asia/Tokyo")


# ══════════════════════════════════════════════
# value generator
# ══════════════════════════════════════════════

class ValueGen:
    """t (elapsed sec) → value。純関数、副作用なし。"""

    def value_at(self, t: float) -> float:
        raise NotImplementedError


@dataclass
class Constant(ValueGen):
    value: float

    def value_at(self, t: float) -> float:
        return float(self.value)


@dataclass
class Ramp(ValueGen):
    """t=0 で start、t=duration_sec で end に線形補間。以降 end 固定。"""
    start: float
    end: float
    duration_sec: float

    def value_at(self, t: float) -> float:
        if t <= 0:
            return float(self.start)
        if t >= self.duration_sec:
            return float(self.end)
        ratio = t / self.duration_sec
        return self.start + ratio * (self.end - self.start)


@dataclass
class Sine(ValueGen):
    """center + amplitude * sin(2π t / period_sec)."""
    center: float
    amplitude: float
    period_sec: float

    def value_at(self, t: float) -> float:
        if self.period_sec <= 0:
            return float(self.center)
        return self.center + self.amplitude * math.sin(
            2.0 * math.pi * t / self.period_sec
        )


@dataclass
class Jitter(ValueGen):
    """base + N(0, sigma)。テスト再現性のため seed 可指定。"""
    base: float
    sigma: float
    seed: int = 0

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)

    def value_at(self, t: float) -> float:
        return self.base + self._rng.gauss(0.0, self.sigma)


def build_generator(spec: dict) -> ValueGen:
    kind = spec.get("kind", "constant")
    if kind == "constant":
        return Constant(value=float(spec["value"]))
    if kind == "ramp":
        return Ramp(
            start=float(spec["start"]),
            end=float(spec["end"]),
            duration_sec=float(spec["duration_sec"]),
        )
    if kind == "sine":
        return Sine(
            center=float(spec["center"]),
            amplitude=float(spec["amplitude"]),
            period_sec=float(spec["period_sec"]),
        )
    if kind == "jitter":
        return Jitter(
            base=float(spec["base"]),
            sigma=float(spec["sigma"]),
            seed=int(spec.get("seed", 0)),
        )
    raise ValueError(f"unknown generator kind: {kind!r}")


# ══════════════════════════════════════════════
# DATA XML 生成
# ══════════════════════════════════════════════

@dataclass
class CCM:
    type: str           # e.g., "InAirTemp"
    room: int
    region: int
    order: int = 1
    priority: int = 15
    suffix: str = ".cMC"   # UECS: A レベル broadcast は .cMC
    gen: ValueGen = field(default_factory=lambda: Constant(0.0))

    def build_data_xml(self, t: float, decimals: int = 2) -> str:
        v = self.gen.value_at(t)
        val_str = f"{v:.{decimals}f}"
        return (
            f'<DATA type="{self.type}{self.suffix}"'
            f' room="{self.room}" region="{self.region}"'
            f' priority="{self.priority}" order="{self.order}">'
            f'{val_str}</DATA>'
        )


def build_packet(datas: list[str]) -> bytes:
    """複数 DATA を UECS 電文の 1 packet に詰める。単発でも同じ形式。"""
    body = "".join(datas)
    doc = f'<?xml version="1.0"?><UECS ver="1.00-E10">{body}</UECS>'
    return doc.encode("utf-8")


# ══════════════════════════════════════════════
# Node (interval で emit する CCM 束)
# ══════════════════════════════════════════════

@dataclass
class Node:
    name: str
    interval_sec: float
    ccms: list[CCM]
    _next_fire: float = 0.0  # relative to sim start

    def due(self, elapsed: float) -> bool:
        return elapsed >= self._next_fire

    def advance(self) -> None:
        self._next_fire += self.interval_sec

    def emit_packets(
        self, elapsed: float, multi_data_per_packet: bool
    ) -> list[bytes]:
        datas = [c.build_data_xml(elapsed) for c in self.ccms]
        if multi_data_per_packet:
            return [build_packet(datas)] if datas else []
        return [build_packet([d]) for d in datas]


def load_scenario(path: Path) -> tuple[list[Node], dict]:
    """yaml から nodes と target 設定を読む。"""
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    target = data.get("target") or {}
    target.setdefault("multicast", "224.0.0.1")
    target.setdefault("port", 16520)

    nodes: list[Node] = []
    for n in data.get("nodes") or []:
        ccms = [
            CCM(
                type=c["type"],
                room=int(c["room"]),
                region=int(c["region"]),
                order=int(c.get("order", 1)),
                priority=int(c.get("priority", 15)),
                suffix=c.get("suffix", ".cMC"),
                gen=build_generator(c["value"]),
            )
            for c in (n.get("ccms") or [])
        ]
        nodes.append(Node(
            name=n["name"],
            interval_sec=float(n.get("interval_sec", 10.0)),
            ccms=ccms,
        ))
    return nodes, target


# ══════════════════════════════════════════════
# UDP 送信 socket
# ══════════════════════════════════════════════

def make_socket(target: str, ttl: int = 1) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    # 224.0.0.0/4 マルチキャストなら TTL 設定 (単純 unicast 時は無害)
    try:
        ttl_bytes = struct.pack("b", ttl)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, ttl_bytes)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
    except OSError:
        pass
    return sock


# ══════════════════════════════════════════════
# runner
# ══════════════════════════════════════════════

def run(
    nodes: list[Node],
    target_host: str,
    target_port: int,
    duration: float,
    multi_data_per_packet: bool = False,
    dry_run: bool = False,
    tick_sec: float = 0.1,
    sender: Callable[[bytes], None] | None = None,
) -> dict:
    """duration 秒間、各 node の interval に従って emit する。

    sender: 送出関数の差し替えポイント (テスト時 stdout 収集用など)。
            None なら実 UDP 送出 (dry_run=True なら 何もしない)。
    Returns: {"packets": int, "bytes": int, "nodes_fired": {name: count}}
    """
    stats = {"packets": 0, "bytes": 0, "nodes_fired": {n.name: 0 for n in nodes}}

    if sender is None:
        if dry_run:
            def _send(p: bytes) -> None:
                sys.stdout.write(f"[dry-run] {len(p)} bytes: {p.decode('utf-8', 'replace')}\n")
            sender = _send
        else:
            sock = make_socket(target_host)
            def _real(p: bytes) -> None:
                sock.sendto(p, (target_host, target_port))
            sender = _real

    stop = {"flag": False}
    def _sig(_a, _b):
        stop["flag"] = True
    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    start = time.time()
    end = start + duration if duration > 0 else float("inf")
    while not stop["flag"] and time.time() < end:
        elapsed = time.time() - start
        any_fired = False
        for node in nodes:
            if node.due(elapsed):
                packets = node.emit_packets(elapsed, multi_data_per_packet)
                for p in packets:
                    sender(p)
                    stats["packets"] += 1
                    stats["bytes"] += len(p)
                stats["nodes_fired"][node.name] += len(packets)
                node.advance()
                any_fired = True
        if not any_fired:
            time.sleep(tick_sec)

    return stats


# ══════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ccm_simulator",
        description="Synthetic UECS-CCM UDP packet generator.",
    )
    p.add_argument("--config", required=True, type=Path,
                   help="scenario yaml")
    p.add_argument("--target", default=None,
                   help="送信先ホスト (default: yaml.target.multicast or 224.0.0.1)")
    p.add_argument("--port", type=int, default=None,
                   help="送信先ポート (default: yaml.target.port or 16520)")
    p.add_argument("--duration", type=float, default=0,
                   help="実行秒数 (0=無限、SIGINT で終了)")
    p.add_argument("--multi-data-per-packet", action="store_true",
                   help="node の全 DATA を 1 packet にまとめる (ArsProut 非互換テスト用)")
    p.add_argument("--dry-run", action="store_true",
                   help="実 UDP 送出せず stdout に payload を出すだけ")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    nodes, target = load_scenario(args.config)
    host = args.target or target["multicast"]
    port = args.port or int(target["port"])

    print(
        f"[sim] target={host}:{port} nodes={len(nodes)} duration={args.duration or 'inf'}s "
        f"multi_data={args.multi_data_per_packet} dry_run={args.dry_run}",
        file=sys.stderr,
    )
    stats = run(
        nodes, host, port, args.duration,
        multi_data_per_packet=args.multi_data_per_packet,
        dry_run=args.dry_run,
    )
    print(
        f"[sim] done: packets={stats['packets']} bytes={stats['bytes']} "
        f"per_node={stats['nodes_fired']}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
