"""agri_relay_simulator.py — CCM actuator (agri-relay-*) 仮想デバイスシミュレータ.

現地 OGMS/agri-relay 系デバイス (灌水/CO2/結露/窓/リレー) の受信側を模擬。
DSL primary executor が生成する制御コマンドを 実 relay 触らずに受け取り、
log + fake state publish する。step 6 (primary 昇格) の safety-first 検証用。

## protocol

UECS-CCM UDP マルチキャスト (224.0.0.1:16520 default) 上で:
    - 受信: `<DATA type="Xxx.rcA" room=1 region=61 order=N>value</DATA>`
      (rcA = 制御コマンド、arsprout-analysis actuator 設計 doc より)
    - 送信: `<DATA type="Xxx.opr" room=1 region=61 order=N>state</DATA>`
      (opr = 現在の operational state)

`.rcA` を受信したら該当 actuator の state を書き換え + jsonl log。
`state_emit_interval_sec` 毎に全 actuator の state を opr type で送出、
これで下流の bridge / capture / uecs_webui が「fake device が生きて反応してる」
様子を live で観察できる。

## YAML config (examples/agri_relay_simulator.example.yaml 参照)

```yaml
device:
  name: agri-relay-192-sim
  room: 1
  region: 71                # 現地 h2 actuator region と一致させる
target:
  multicast: 224.0.0.1
  port: 16520
  bind_ip: 0.0.0.0

actuators:
  - name: irrigation
    cmd_type: Irrirc A      # 受信 command type (先頭 . なしの suffix なし形)
    state_type: Irriopr     # 送信 state type
    order: 1
    initial: 0
  - name: relay_ch1
    cmd_type: RelayrcA
    state_type: Relayopr
    order: 1
    initial: 0

state_emit_interval_sec: 10.0
log_path: /var/log/agri-relay-sim.jsonl
```

## Usage

```bash
python3 agri_relay_simulator.py --config scenario.yaml
python3 agri_relay_simulator.py --config scenario.yaml --dry-run   # 受信 log のみ、送信しない
python3 agri_relay_simulator.py --config scenario.yaml --duration 60   # 60s で終了
```

依存: 標準ライブラリ + pyyaml。
"""

from __future__ import annotations

import argparse
import json
import re
import signal
import socket
import struct
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import yaml


_JST = ZoneInfo("Asia/Tokyo")
_XML_DECL_RE = re.compile(r"<\?xml[^?]*\?>", re.I)


# ══════════════════════════════════════════════
# XML パーサ (ccm_capture と同構造、依存を避けるため inline)
# ══════════════════════════════════════════════

def _strip_suffix(t: str) -> str:
    """`.cMC` / `.mC` / `.MC` を落とす。"""
    for s in (".cMC", ".mC", ".MC"):
        if t.endswith(s):
            return t[: -len(s)]
    return t


def parse_data_elements(payload: str) -> list[dict[str, Any]]:
    """パケット payload から <DATA> 要素を全部拾う (壊れた packet は空 list)。"""
    src = _XML_DECL_RE.sub("", payload).strip()
    if not src:
        return []
    wrapped = f"<root>{src}</root>"
    try:
        root = ET.fromstring(wrapped)
    except ET.ParseError:
        return []
    out: list[dict[str, Any]] = []
    for elem in root.iter("DATA"):
        attrs = elem.attrib
        typ_raw = attrs.get("type", "")
        typ = _strip_suffix(typ_raw)
        text = (elem.text or "").strip()
        try:
            value: Any = float(text) if text else None
        except ValueError:
            value = text or None
        out.append({
            "type_raw": typ_raw,
            "type": typ,
            "room": _to_int(attrs.get("room")),
            "region": _to_int(attrs.get("region")),
            "order": _to_int(attrs.get("order")),
            "priority": _to_int(attrs.get("priority")),
            "value": value,
        })
    return out


def _to_int(s: str | None) -> int | None:
    if s is None or s == "":
        return None
    try:
        return int(s)
    except ValueError:
        return None


# ══════════════════════════════════════════════
# XML builder (ccm_simulator と同構造)
# ══════════════════════════════════════════════

def build_data_xml(
    ccm_type: str, room: int, region: int, order: int, priority: int, value: Any,
) -> str:
    if isinstance(value, float) and value.is_integer():
        val_str = str(int(value))
    elif isinstance(value, float):
        val_str = f"{value:.3f}"
    else:
        val_str = str(value)
    return (
        f'<DATA type="{ccm_type}"'
        f' room="{room}" region="{region}"'
        f' priority="{priority}" order="{order}">'
        f'{val_str}</DATA>'
    )


def build_packet(datas: list[str]) -> bytes:
    body = "".join(datas)
    doc = f'<?xml version="1.0"?><UECS ver="1.00-E10">{body}</UECS>'
    return doc.encode("utf-8")


# ══════════════════════════════════════════════
# actuator 定義 + state store
# ══════════════════════════════════════════════

@dataclass
class Actuator:
    """1 個の仮想 actuator (灌水電磁弁 / relay ch / 窓 wid etc)。"""
    name: str
    cmd_type: str           # 受信 command type (rcA suffix なしの形)
    state_type: str         # 送信 state type (opr 相当)
    order: int
    initial: float | int
    priority: int = 15
    # runtime state
    current: float | int = field(init=False)
    last_cmd_at: float | None = field(init=False, default=None)
    last_cmd_source: str | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        self.current = self.initial

    def apply_cmd(self, value: Any, source_ip: str | None, now_ts: float) -> None:
        """受信 command で state 更新。value 型は cmd に従って float or int。"""
        try:
            self.current = float(value) if value is not None else self.current
        except (TypeError, ValueError):
            # 数値変換失敗は string で保持 (ArsProut 特殊 payload 用)
            self.current = value
        self.last_cmd_at = now_ts
        self.last_cmd_source = source_ip


# ══════════════════════════════════════════════
# config loader
# ══════════════════════════════════════════════

@dataclass
class Config:
    device_name: str
    room: int
    region: int
    multicast: str
    port: int
    bind_ip: str
    actuators: list[Actuator]
    state_emit_interval_sec: float
    log_path: str | None


def load_config(path: Path) -> Config:
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    device = data.get("device") or {}
    target = data.get("target") or {}
    actuators = [
        Actuator(
            name=a["name"],
            cmd_type=a["cmd_type"],
            state_type=a["state_type"],
            order=int(a.get("order", 1)),
            initial=a.get("initial", 0),
            priority=int(a.get("priority", 15)),
        )
        for a in (data.get("actuators") or [])
    ]
    return Config(
        device_name=str(device.get("name", "agri-relay-sim")),
        room=int(device.get("room", 1)),
        region=int(device.get("region", 71)),
        multicast=str(target.get("multicast", "224.0.0.1")),
        port=int(target.get("port", 16520)),
        bind_ip=str(target.get("bind_ip", "0.0.0.0")),
        actuators=actuators,
        state_emit_interval_sec=float(data.get("state_emit_interval_sec", 10.0)),
        log_path=data.get("log_path"),
    )


# ══════════════════════════════════════════════
# UDP sockets
# ══════════════════════════════════════════════

def make_recv_socket(multicast: str, port: int, bind_ip: str = "0.0.0.0") -> socket.socket:
    """マルチキャスト受信 socket を join して返す。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except (OSError, AttributeError):
        pass
    sock.bind((bind_ip, port))
    # multicast group join
    mreq = struct.pack("4s4s", socket.inet_aton(multicast), socket.inet_aton(bind_ip))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    sock.settimeout(0.5)  # for graceful shutdown polling
    return sock


def make_send_socket(ttl: int = 1) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, struct.pack("b", ttl))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
    except OSError:
        pass
    return sock


# ══════════════════════════════════════════════
# core loop
# ══════════════════════════════════════════════

def handle_packet(
    payload: str,
    src_ip: str | None,
    actuators: dict[tuple[str, int], Actuator],
    room_filter: int,
    region_filter: int,
    now_ts: float,
) -> list[dict[str, Any]]:
    """受信 payload を parse、該当 actuator の state 更新、log entry を返す。

    actuators registry は (cmd_type, order) key。同一 cmd_type 内の複数 order
    (現地の VenSdWinrcA order=1/2 など) を分離管理する。
    order 欠落 packet は UECS 仕様に従い order=1 として扱う。
    """
    entries: list[dict[str, Any]] = []
    for data in parse_data_elements(payload):
        # rcA (command) だけを処理、opr (自分の送信も含む) は無視
        if not data["type"].endswith("rcA") and not data["type_raw"].endswith("rcA"):
            continue
        # room/region が config と違うものは無視 (他の device 宛て)
        if data.get("room") is not None and data["room"] != room_filter:
            continue
        if data.get("region") is not None and data["region"] != region_filter:
            continue
        order = data.get("order")
        if order is None:
            order = 1  # UECS 仕様: order 欠落は主系統 1
        key = (data["type"], order)
        actuator = actuators.get(key)
        if actuator is None:
            # 未定義の rcA を受信した (config で扱ってない actuator or order) → log だけ残す
            entries.append({
                "ts": datetime.now(_JST).isoformat(),
                "event": "unmapped_cmd",
                "src_ip": src_ip,
                "type": data["type_raw"],
                "room": data["room"],
                "region": data["region"],
                "order": data["order"],
                "value": data["value"],
            })
            continue
        prev = actuator.current
        actuator.apply_cmd(data["value"], src_ip, now_ts)
        entries.append({
            "ts": datetime.now(_JST).isoformat(),
            "event": "cmd_applied",
            "src_ip": src_ip,
            "actuator": actuator.name,
            "type": data["type_raw"],
            "order": actuator.order,
            "prev": prev,
            "new": actuator.current,
        })
    return entries


def emit_state_packets(
    cfg: Config,
    actuators_by_name: dict[str, Actuator],
    sender: Callable[[bytes, str, int], None],
) -> int:
    """全 actuator の state (opr type) を 1 packet ずつ送出。送信 packet 数を返す。"""
    n = 0
    for act in actuators_by_name.values():
        xml = build_data_xml(
            ccm_type=act.state_type,
            room=cfg.room,
            region=cfg.region,
            order=act.order,
            priority=act.priority,
            value=act.current,
        )
        pkt = build_packet([xml])
        sender(pkt, cfg.multicast, cfg.port)
        n += 1
    return n


def _write_log(entries: list[dict[str, Any]], log_path: str | None) -> None:
    if not log_path or not entries:
        return
    p = Path(log_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def run(
    cfg: Config,
    duration: float | None = None,
    dry_run: bool = False,
    tick_sec: float = 0.2,
    recv_sock: socket.socket | None = None,
    sender: Callable[[bytes, str, int], None] | None = None,
    now_provider: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """simulator メイン loop。テスト時は recv_sock / sender を差し替え可能。

    duration=None は無限 loop (SIGINT で止まる)。
    Returns: {"cmds_received": int, "state_packets_sent": int, "unmapped": int}
    """
    # 名前ではなく cmd_type key で lookup (受信側は type で識別)
    # (cmd_type, order) key で actuator を registry 化。
    # 同一 cmd_type の複数 order (現地 VenSdWinrcA order=1/2 etc) を正しく分離
    by_cmd_type: dict[tuple[str, int], Actuator] = {
        (a.cmd_type, a.order): a for a in cfg.actuators
    }
    by_name: dict[str, Actuator] = {a.name: a for a in cfg.actuators}

    stats = {"cmds_received": 0, "state_packets_sent": 0, "unmapped": 0}

    own_recv_sock = False
    if recv_sock is None:
        recv_sock = make_recv_socket(cfg.multicast, cfg.port, cfg.bind_ip)
        own_recv_sock = True
    send_sock: socket.socket | None = None
    if sender is None:
        if dry_run:
            def _send(pkt: bytes, host: str, port: int) -> None:
                sys.stdout.write(f"[dry-run send] {host}:{port} {len(pkt)}B: {pkt.decode('utf-8', 'replace')}\n")
        else:
            send_sock = make_send_socket()
            def _send(pkt: bytes, host: str, port: int) -> None:
                assert send_sock is not None
                send_sock.sendto(pkt, (host, port))
        sender = _send

    stop = {"flag": False}
    def _handler(signum, frame):
        stop["flag"] = True
    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)

    start = now_provider()
    next_state_emit = start + cfg.state_emit_interval_sec
    # 起動時に initial state を 1 回 emit (下流に fake device を認識させる)
    initial_pkts = emit_state_packets(cfg, by_name, sender)
    stats["state_packets_sent"] += initial_pkts

    try:
        while not stop["flag"]:
            now = now_provider()
            if duration is not None and now - start >= duration:
                break

            # 受信 (blocking with timeout=0.5s from make_recv_socket)
            try:
                data, addr = recv_sock.recvfrom(4096)
                payload = data.decode("utf-8", "replace")
                src_ip = addr[0] if addr else None
                entries = handle_packet(
                    payload, src_ip, by_cmd_type, cfg.room, cfg.region, now,
                )
                if entries:
                    _write_log(entries, cfg.log_path)
                    for e in entries:
                        if e["event"] == "cmd_applied":
                            stats["cmds_received"] += 1
                        elif e["event"] == "unmapped_cmd":
                            stats["unmapped"] += 1
            except socket.timeout:
                pass
            except OSError:
                # non-fatal socket 系エラー
                pass

            # 定期 state emit
            if now >= next_state_emit:
                stats["state_packets_sent"] += emit_state_packets(cfg, by_name, sender)
                next_state_emit = now + cfg.state_emit_interval_sec

            # 短い sleep で CPU 節約
            time.sleep(tick_sec)
    finally:
        if own_recv_sock:
            recv_sock.close()
        if send_sock is not None:
            send_sock.close()

    return stats


# ══════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="agri_relay_simulator",
        description="CCM actuator (agri-relay-*) 仮想デバイスシミュレータ",
    )
    p.add_argument("--config", required=True, type=Path, help="scenario yaml path")
    p.add_argument("--duration", type=float, default=None,
                   help="実行時間 (秒)、指定なしは SIGINT まで無限")
    p.add_argument("--dry-run", action="store_true",
                   help="送信 socket 使わず stdout に printer、log は書く")
    args = p.parse_args(argv)

    cfg = load_config(args.config)
    print(
        f"[agri-relay-sim] device={cfg.device_name} room={cfg.room} region={cfg.region}"
        f" actuators={[a.name for a in cfg.actuators]}"
        f" listen={cfg.multicast}:{cfg.port} emit_interval={cfg.state_emit_interval_sec}s",
        file=sys.stderr,
    )
    stats = run(cfg, duration=args.duration, dry_run=args.dry_run)
    print(f"[agri-relay-sim] done: {stats}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
