"""ccm_capture.py — UECS-CCM UDP マルチキャストの passive capture / inspect ツール.

`ccm_mqtt_bridge` を挟む前段で「そもそも CCM が何を流しているか」を眺めるためのもの。
UDP マルチキャスト (default 224.0.0.1:16520) を join、受信パケットを 1 行 jsonl で
記録する。フィルタ (`--type` / `--room` / `--region` / `--ip`) を通して観察対象を
絞り込める。

現地で判明している UECS 実装の癖:
  - ArsProut 実装は **1 パケット 1 DATA** でないとデータを取りこぼす
    → capture 側で 1 パケット内に DATA が複数入っていたら WARN 出力 (bridge 側で
      問題起きうる)
  - `type` 属性末尾に `.cMC` / `.mC` / `.MC` の suffix が付くことがある (UARDECS 仕様)
  - `order` 属性が欠落したら主系統=1 として扱う (仕様上は必須だが送信側都合で欠ける)

依存: 標準ライブラリのみ (yaml/paho 不要)。

Subcommands:
  capture  live capture → jsonl
  inspect  jsonl 再表示 / 集計 (post-mortem)
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
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo


_JST = ZoneInfo("Asia/Tokyo")

# ══════════════════════════════════════════════
# DATA XML パーサ
# ══════════════════════════════════════════════

# XML 宣言除去用 (ET が multi-root を許さないので後で <root> でラップするため)
_XML_DECL_RE = re.compile(r"<\?xml[^?]*\?>", re.I)


def strip_suffix(t: str) -> str:
    for s in (".cMC", ".mC", ".MC"):
        if t.endswith(s):
            return t[: -len(s)]
    return t


def parse_data_elements(payload: str) -> list[dict]:
    """パケット payload から <DATA> 要素を全部拾う。

    - 属性順序に依存しない (ElementTree は attribute を dict で持つ)
    - `type` 末尾の `.cMC` / `.mC` / `.MC` は strip して `type` に、raw は `type_raw` に
    - 属性欠落や非数値は None 保持 — 送信側都合の欠損 (ArsProut は order 落とすなど)
      を落とさない
    - パース失敗時は空 list (壊れたパケットを capture 全体停止に繋げない)
    """
    src = _XML_DECL_RE.sub("", payload).strip()
    if not src:
        return []
    # 単発の <DATA .../> でも複数要素でも受けられるよう <root> でラップ
    wrapped = f"<root>{src}</root>"
    try:
        root = ET.fromstring(wrapped)
    except ET.ParseError:
        return []

    out: list[dict] = []
    for elem in root.iter("DATA"):
        attrs = elem.attrib
        typ_raw = attrs.get("type", "")
        typ = strip_suffix(typ_raw)
        text = (elem.text or "").strip()
        try:
            value_num: float | str | None = float(text) if text else None
        except ValueError:
            value_num = text or None
        out.append({
            "type_raw": typ_raw,
            "type": typ,
            "room": _to_int_or_none(attrs.get("room")),
            "region": _to_int_or_none(attrs.get("region")),
            "order": _to_int_or_none(attrs.get("order")),
            "priority": _to_int_or_none(attrs.get("priority")),
            "value": value_num,
        })
    return out


def _to_int_or_none(s: str | None) -> int | None:
    if s is None or s == "":
        return None
    try:
        return int(s)
    except ValueError:
        return None


# ══════════════════════════════════════════════
# フィルタ
# ══════════════════════════════════════════════

class Filter:
    def __init__(
        self,
        types: list[str] | None = None,
        rooms: list[int] | None = None,
        regions: list[int] | None = None,
        ips: list[str] | None = None,
    ) -> None:
        self.types = set(types or [])
        self.rooms = set(rooms or [])
        self.regions = set(regions or [])
        self.ips = set(ips or [])

    def match_packet(self, ip: str) -> bool:
        if self.ips and ip not in self.ips:
            return False
        return True

    def match_data(self, elem: dict) -> bool:
        if self.types and elem["type"] not in self.types:
            return False
        if self.rooms and elem["room"] not in self.rooms:
            return False
        if self.regions and elem["region"] not in self.regions:
            return False
        return True

    @property
    def has_data_filter(self) -> bool:
        return bool(self.types or self.rooms or self.regions)


# ══════════════════════════════════════════════
# capture 本体
# ══════════════════════════════════════════════

def make_multicast_socket(mcast: str, port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", port))
    mreq = struct.pack("4sl", socket.inet_aton(mcast), socket.INADDR_ANY)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    except OSError as e:
        # 224.0.0.1 (all-hosts) はホスト bind だけで届くケースがあるので致命ではない
        print(f"[capture] join warn: {e} (proceeding on bind-only)",
              file=sys.stderr)
    return sock


def build_entry(
    ip: str,
    raw: str,
    elements: list[dict],
    include_raw: bool,
) -> dict:
    ts = datetime.now(_JST).isoformat(timespec="microseconds")
    entry = {
        "ts": ts,
        "src_ip": ip,
        "raw_len": len(raw),
        "num_data": len(elements),
        "elements": elements,
    }
    # arsprout 互換性違反の検出
    if len(elements) > 1:
        entry["warn"] = "multi_data_in_single_packet"
    if include_raw:
        entry["raw"] = raw
    return entry


def cmd_capture(args: argparse.Namespace) -> int:
    """live capture — SIGINT で終了、jsonl に追記していく。"""
    flt = Filter(
        types=args.type or None,
        rooms=args.room or None,
        regions=args.region or None,
        ips=args.ip or None,
    )
    sock = make_multicast_socket(args.multicast, args.port)

    out_fp = sys.stdout if args.out == "-" else Path(args.out).open("a", encoding="utf-8")
    close_needed = out_fp is not sys.stdout

    print(
        f"[capture] listening {args.multicast}:{args.port} "
        f"(filter: type={sorted(flt.types)} room={sorted(flt.rooms)} "
        f"region={sorted(flt.regions)} ip={sorted(flt.ips)}) → {args.out}",
        file=sys.stderr,
    )

    stop = {"flag": False}
    def _sig(_a, _b):
        stop["flag"] = True
    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    deadline = time.time() + args.duration if args.duration else None
    count = 0
    warned = 0
    try:
        while not stop["flag"]:
            # duration/count 用 select は不要 — recv でブロック、SIGINT で抜ける
            if deadline and time.time() >= deadline:
                break
            try:
                sock.settimeout(1.0)
                data, addr = sock.recvfrom(8192)
            except TimeoutError:
                continue
            ip = addr[0]
            if not flt.match_packet(ip):
                continue
            text = data.decode("utf-8", "replace")
            elements = parse_data_elements(text)
            if flt.has_data_filter:
                elements = [e for e in elements if flt.match_data(e)]
                if not elements:
                    continue
            entry = build_entry(ip, text, elements, args.raw)
            if "warn" in entry:
                warned += 1
                print(
                    f"[capture] WARN {ip} sent {entry['num_data']} DATA in one packet "
                    f"(arsprout 実装だと取りこぼしうる)",
                    file=sys.stderr,
                )
            out_fp.write(json.dumps(entry, ensure_ascii=False) + "\n")
            out_fp.flush()
            count += 1
            if args.count and count >= args.count:
                break
    finally:
        if close_needed:
            out_fp.close()
        sock.close()
    print(
        f"[capture] done: packets={count} multi_data_warn={warned}",
        file=sys.stderr,
    )
    return 0


# ══════════════════════════════════════════════
# inspect (post-mortem 集計)
# ══════════════════════════════════════════════

def _load_jsonl(path: Path) -> Iterable[dict]:
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def cmd_inspect(args: argparse.Namespace) -> int:
    entries = list(_load_jsonl(Path(args.log)))
    flt = Filter(
        types=args.type or None,
        rooms=args.room or None,
        regions=args.region or None,
        ips=args.ip or None,
    )
    ip_counts: Counter[str] = Counter()
    type_counts: Counter[str] = Counter()
    room_region: Counter[tuple[int | None, int | None]] = Counter()
    multi_data = 0
    total_packets = 0
    total_elems = 0
    first_ts = last_ts = None
    for e in entries:
        if not flt.match_packet(e["src_ip"]):
            continue
        elems = [d for d in e["elements"] if flt.match_data(d)]
        if flt.has_data_filter and not elems:
            continue
        total_packets += 1
        total_elems += len(elems)
        if e.get("warn") == "multi_data_in_single_packet":
            multi_data += 1
        ip_counts[e["src_ip"]] += 1
        for d in elems:
            type_counts[d["type"]] += 1
            room_region[(d["room"], d["region"])] += 1
        ts = e.get("ts")
        if ts:
            if first_ts is None or ts < first_ts:
                first_ts = ts
            if last_ts is None or ts > last_ts:
                last_ts = ts

    print("═" * 60)
    print(f"range      : {first_ts}  →  {last_ts}")
    print(f"packets    : {total_packets}")
    print(f"DATA elems : {total_elems}")
    print(f"multi_data : {multi_data}   ⚠ arsprout 実装だと取りこぼし可能性")
    print()
    print("── by src_ip ──")
    for ip, n in ip_counts.most_common():
        print(f"  {ip:20s} {n}")
    print()
    print("── by type ──")
    for typ, n in type_counts.most_common(20):
        print(f"  {typ:20s} {n}")
    print()
    print("── (room, region) 分布 ──")
    for (rm, rg), n in room_region.most_common(20):
        print(f"  ({rm}, {rg}):  {n}")
    return 0


# ══════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ccm_capture",
        description="UECS-CCM UDP multicast passive capture / inspect.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    # capture
    pc = sub.add_parser("capture", help="live capture to jsonl")
    pc.add_argument("--multicast", default="224.0.0.1")
    pc.add_argument("--port", type=int, default=16520)
    pc.add_argument("--out", default="-",
                    help='jsonl 出力 (default "-" = stdout)')
    pc.add_argument("--raw", action="store_true",
                    help="生 payload text も each entry に含める")
    pc.add_argument("--count", type=int, default=0,
                    help="N パケットで終了 (0=infinite)")
    pc.add_argument("--duration", type=int, default=0,
                    help="N 秒で終了 (0=infinite)")
    pc.add_argument("--type", action="append",
                    help="type フィルタ (複数指定可)")
    pc.add_argument("--room", type=int, action="append",
                    help="room フィルタ (複数指定可)")
    pc.add_argument("--region", type=int, action="append",
                    help="region フィルタ (複数指定可)")
    pc.add_argument("--ip", action="append",
                    help="発信元 IP フィルタ (複数指定可)")
    pc.set_defaults(func=cmd_capture)

    # inspect
    pi = sub.add_parser("inspect", help="summarize a captured jsonl")
    pi.add_argument("--log", required=True)
    pi.add_argument("--type", action="append")
    pi.add_argument("--room", type=int, action="append")
    pi.add_argument("--region", type=int, action="append")
    pi.add_argument("--ip", action="append")
    pi.set_defaults(func=cmd_inspect)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
