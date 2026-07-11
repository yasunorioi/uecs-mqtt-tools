#!/usr/bin/env python3
# UECS-CCM → MQTT ブリッジ（一方向）
#
# UECS-CCM (UDP マルチキャスト 224.0.0.1:16520) を受信し、指定 MQTT 命名規約に沿って
# republish する。Node-RED 版 (docs/uecs-mqtt-bridge-generator.md) の Python 後継。
# broker 同梱の mosquitto_pub を subprocess 呼び出しで使うので、paho など pip 依存は
# pyyaml のみ。
#
# --- UECS-CCM 仕様準拠について ---
# CCM のデータ identity は (type, room, region, ORDER) の4要素。order は「同じ
# type/room/region に複数台ある場合の連番」で、これを落とすと別個体が1トピックに
# 潰れて last-writer-wins する。一方 agriha 命名規約(§0.1)は
# `agriha/<scope>/<category>/<type>` ＝「1スコープ1型1トピック」前提。両者を
# 両立させるため:
#   * order=1（既定の主系統）は素のトピック（既存購読との互換維持）
#   * order>=2 は `.../{type}/{order}` を付けて multi-order を保存
#   * 異なる発信元(ip/room/region/order)が同一トピックに書こうとしたら WARN ログ
#     （黙って上書きしない）
# また region 単独では発信元ハウスを一意に決められない現地癖 (例: 特定 .80 は
# InAir* を region12、Soil*/Pulse/IntgRadiation を region11 で送るなど) があるため、
# sender_override で発信元IPごとの (room,region)->scope 上書きを用意した。
#
# 使い方:
#   python3 ccm_mqtt_bridge.py --config /etc/uecs-mqtt-bridge/config.yaml
#
# 設定サンプル: examples/scope_map.example.yaml

import argparse
import json
import re
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

import yaml


# type -> unit（agriha §0.4 の {value,unit,ts}）。無い型は ""。
# UECS 標準 type が中心なので config には出さず定数固定。
UNIT = {
    "InAirTemp": "C", "InAirHumid": "%", "InAirCO2": "ppm", "InAirPressure": "hPa",
    "InAirAbsHumid": "g m-3", "InAirDP": "C", "InAirHD": "kPa", "InRadiation": "kW m-2",
    "IntgRadiation": "MJ m-2", "Pulse": "", "SoilTemp": "C", "SoilWC": "%", "SoilEC": "dS m-1",
    "WAirTemp": "C", "WAirHumid": "%", "WWindSpeed": "m s-1", "WWindDir16": "16dir",
    "WRainfallAmt": "mm", "WRadiation": "kW m-2", "WRadInteg": "MJ m-2",
}

DATA_RE = re.compile(
    r'<DATA\s+type="([^"]*)"'
    r'(?:[^>]*?\broom="([^"]*)")?'
    r'(?:[^>]*?\bregion="([^"]*)")?'
    r'(?:[^>]*?\border="([^"]*)")?'
    r'(?:[^>]*?\bpriority="([^"]*)")?'
    r'[^>]*>([^<]*)</DATA>', re.I)

# センサー値でない型は除外（cnd=ノード生存通知 等）
SKIP_TYPES = {"cnd"}


# ══════════════════════════════════════════════
# config loading
# ══════════════════════════════════════════════

class Config:
    """外部 yaml から scope_map / sender_override / broker などをまとめて持つ。"""

    def __init__(self, raw: dict) -> None:
        ccm = raw.get("ccm") or {}
        self.mcast = ccm.get("multicast", "224.0.0.1")
        self.port = int(ccm.get("port", 16520))

        mqtt = raw.get("mqtt") or {}
        self.broker = mqtt.get("broker", "localhost")
        self.mqtt_port = int(mqtt.get("port", 1883))
        self.topic_prefix = mqtt.get("topic_prefix", "agriha")

        # scope_map: list of entries → dict[(room,region) -> (scope, category)]
        self.scope_map: dict[tuple[int, int], tuple] = {}
        for entry in raw.get("scope_map") or []:
            key = (int(entry["room"]), int(entry["region"]))
            self.scope_map[key] = (entry["scope"], entry["category"])

        # sender_override: list → dict[ip -> {(room,region) -> (scope, category)}]
        self.sender_override: dict[str, dict[tuple[int, int], tuple]] = {}
        for entry in raw.get("sender_override") or []:
            ip = entry["ip"]
            key = (int(entry["room"]), int(entry["region"]))
            self.sender_override.setdefault(ip, {})[key] = (
                entry["scope"], entry["category"]
            )

    @classmethod
    def load(cls, path: Path) -> "Config":
        with path.open(encoding="utf-8") as f:
            return cls(yaml.safe_load(f) or {})


def resolve_scope(cfg: Config, ip: str, room: int, region: int):
    # 発信元IP上書き → 基本 scope_map の順で (scope, category) を決める。
    ov = cfg.sender_override.get(ip)
    if ov and (room, region) in ov:
        return ov[(room, region)]
    return cfg.scope_map.get((room, region))


def strip_suffix(t: str) -> str:
    for s in (".cMC", ".mC", ".MC"):
        if t.endswith(s):
            return t[: -len(s)]
    return t


def build_topic(prefix: str, scope, cat: str, typ: str, order: int) -> str:
    # 1スコープ1型を守り、order=1 は素のトピック、order>=2 のみ /{order} 付与。
    if order and order != 1:
        return f"{prefix}/{scope}/{cat}/{typ}/{order}"
    return f"{prefix}/{scope}/{cat}/{typ}"


def publish(cfg: Config, topic: str, payload: str) -> None:
    # broker 同梱 mosquitto_pub を使用（依存追加なし）。QoS1・retain。
    subprocess.run(
        ["mosquitto_pub", "-h", cfg.broker, "-p", str(cfg.mqtt_port),
         "-t", topic, "-m", payload, "-q", "1", "-r"],
        check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


# ══════════════════════════════════════════════
# main loop
# ══════════════════════════════════════════════

def run(cfg: Config) -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", cfg.port))
    mreq = struct.pack("4sl", socket.inet_aton(cfg.mcast), socket.INADDR_ANY)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    except OSError as e:
        print(f"[bridge] join warn: {e} (all-hosts は bind のみで届くことが多い)",
              flush=True)

    print(f"[bridge] CCM {cfg.mcast}:{cfg.port} -> MQTT {cfg.broker}:{cfg.mqtt_port}"
          f" (topic_prefix={cfg.topic_prefix}, via mosquitto_pub)", flush=True)
    seen: dict[str, object] = {}   # topic -> value（値変化のみログ）
    owner: dict[str, tuple] = {}   # topic -> (ip, room, region, order)（衝突検出）
    while True:
        data, addr = sock.recvfrom(8192)
        ip = addr[0]
        try:
            text = data.decode("utf-8", "replace")
        except Exception:
            continue
        for m in DATA_RE.finditer(text):
            typ, room, region, order, priority, val = m.groups()
            typ = strip_suffix(typ)
            if typ in SKIP_TYPES:
                continue
            try:
                room_i = int(room or 0)
                region_i = int(region or 0)
                order_i = int(order or 1)   # UECS order 欠落時は主系統=1 扱い
            except ValueError:
                continue
            sc = resolve_scope(cfg, ip, room_i, region_i)
            if sc is None:
                continue
            scope, cat = sc
            topic = build_topic(cfg.topic_prefix, scope, cat, typ, order_i)

            # 衝突検出
            src = (ip, room_i, region_i, order_i)
            prev = owner.get(topic)
            if prev is not None and prev != src:
                print(
                    f"[bridge] WARN collision on {topic}: {prev} vs {src} "
                    f"(order/region 取り違えの可能性。scope_map/sender_override 要確認)",
                    flush=True,
                )
            owner[topic] = src

            try:
                value = float(val)
            except ValueError:
                value = val.strip()
            publish(cfg, topic, json.dumps(
                {"value": value, "unit": UNIT.get(typ, ""), "ts": int(time.time())}
            ))
            if seen.get(topic) != value:
                seen[topic] = value
                pri = f" pri={priority}" if priority else ""
                print(f"  {ip:<15} {topic} = {value}{pri}", flush=True)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ccm_mqtt_bridge")
    p.add_argument("--config", required=True, type=Path,
                   help="yaml config path (see examples/scope_map.example.yaml)")
    args = p.parse_args(argv)
    cfg = Config.load(args.config)
    if not cfg.scope_map:
        print("[bridge] WARN: scope_map is empty — nothing will be published",
              file=sys.stderr)
    return run(cfg)


if __name__ == "__main__":
    sys.exit(main() or 0)
