"""uecs_webui.py — 稼働中の bridge MQTT を LAN ブラウザで眺める簡易 WebUI.

`ccm_mqtt_bridge` が publish する `<prefix>/#` を subscribe し、topic tree を
group 分けして live 表示する。トピックの age (最終更新からの秒数)、collision
(同 topic を異なる source が上書き)、scope 別グループ (h1/h2/h3/farm) を可視化。

技術選定 (hobby scope):
  - FastAPI + Jinja2 (uecs-llm daemon deps に既にあり)
  - paho-mqtt 背景 thread → in-memory dict (DB 不要)
  - HTMX で /partial を 2 秒 poll、テンプレは 1 ファイル埋め込み
  - LAN-only bind、auth 無し (canopy autoindex と同 policy)

Usage:
  python3 uecs_webui.py \\
      --broker 100.102.95.37 --broker-port 1883 \\
      --topic-prefix agriha \\
      --listen 0.0.0.0 --port 8090

CLI で broker 接続情報と listen 先を指定。systemd 常駐前提。
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import signal
import sys
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


_JST = ZoneInfo("Asia/Tokyo")
logger = logging.getLogger("uecs_webui")


# ══════════════════════════════════════════════
# state model
# ══════════════════════════════════════════════

@dataclass
class TopicEntry:
    topic: str
    value: Any
    unit: str
    ts: float          # ペイロード内の unix ts (broker 側)
    received_at: float  # webui 到着時刻 (server 側)
    payload_raw: str    # 生 JSON (parse 失敗時の debug 用)


@dataclass
class State:
    topics: dict[str, TopicEntry] = field(default_factory=dict)
    collisions: deque = field(default_factory=lambda: deque(maxlen=200))
    subscribe_started_at: float | None = None
    total_messages: int = 0
    last_error: str | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def on_message(self, topic: str, raw: bytes) -> None:
        now = time.time()
        try:
            text = raw.decode("utf-8", "replace")
            data = json.loads(text) if text else {}
        except json.JSONDecodeError:
            data, text = {}, raw.decode("utf-8", "replace")

        # bridge payload は {value, unit, ts} 前提だが、実運用では
        # 数値リテラルや配列を素で流す publisher (agriha-controller/logic/*
        # 等) が居るので dict 以外は payload そのものを value 扱いにする
        if isinstance(data, dict):
            value = data.get("value")
            unit = str(data.get("unit", ""))
            try:
                ts = float(data.get("ts") or 0)
            except (TypeError, ValueError):
                ts = 0.0
        else:
            value = data
            unit = ""
            ts = 0.0

        entry = TopicEntry(
            topic=topic,
            value=value,
            unit=unit,
            ts=ts,
            received_at=now,
            payload_raw=text,
        )
        with self._lock:
            self.total_messages += 1
            prev = self.topics.get(topic)
            # 値変化を collision として扱わない (value change は正常)
            # payload_raw が完全一致で無いのに ts が同じ = 別 source が来てる可能性
            if prev and prev.ts == entry.ts and prev.value != entry.value:
                self.collisions.appendleft({
                    "topic": topic,
                    "prev_value": prev.value,
                    "new_value": entry.value,
                    "ts_iso": datetime.now(_JST).isoformat(timespec="seconds"),
                })
            self.topics[topic] = entry


# ══════════════════════════════════════════════
# grouping / rendering helpers
# ══════════════════════════════════════════════

def group_topics(
    entries: list[TopicEntry], prefix: str
) -> dict[str, dict[str, list[TopicEntry]]]:
    """`prefix/scope/category/type` → grouped[scope][category] = [entries]."""
    grouped: dict[str, dict[str, list[TopicEntry]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for e in entries:
        parts = e.topic.split("/")
        if len(parts) < 4 or parts[0] != prefix:
            grouped["_other"]["_"].append(e)
            continue
        scope = parts[1]
        cat = parts[2]
        grouped[scope][cat].append(e)
    return grouped


def format_age(now: float, ts: float) -> tuple[str, str]:
    """(表示文字列, css class name) を返す。"""
    if ts <= 0:
        return ("—", "unknown")
    age = now - ts
    if age < 30:
        return (f"{age:.0f}s", "fresh")
    if age < 300:
        return (f"{age:.0f}s", "warm")
    if age < 3600:
        return (f"{age/60:.0f}m", "stale")
    return (f"{age/3600:.1f}h", "dead")


# ══════════════════════════════════════════════
# HTML template (self-contained)
# ══════════════════════════════════════════════

_INDEX_HTML = """<!doctype html>
<html lang="ja"><head>
<meta charset="utf-8">
<title>uecs webui — {broker}/{prefix}</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<script src="https://unpkg.com/htmx.org@2.0.4"></script>
<style>
 body{{font-family:system-ui,sans-serif;margin:1rem;color:#222;max-width:1300px}}
 h1{{font-size:1.2rem;margin:0 0 .4rem}}
 .meta{{color:#666;font-size:.85rem;margin-bottom:.8rem}}
 .scope{{background:#f5f5f5;border-radius:.5rem;padding:.6rem .8rem;margin-bottom:.6rem}}
 .scope h2{{font-size:1rem;margin:0 0 .3rem;color:#333}}
 .cat{{margin:.4rem 0 .6rem;padding-left:.6rem;border-left:3px solid #ccc}}
 .cat h3{{font-size:.9rem;margin:0 0 .2rem;color:#555}}
 table{{border-collapse:collapse;font-size:.85rem;width:100%}}
 th,td{{border:1px solid #ddd;padding:.15rem .4rem;text-align:left}}
 th{{background:#eee;font-size:.8rem}}
 td.age.fresh{{background:#eafce8;color:#0a7}}
 td.age.warm{{background:#fff8e0;color:#a80}}
 td.age.stale{{background:#ffe8e0;color:#c33}}
 td.age.dead{{background:#e8e8e8;color:#888}}
 code{{font-family:ui-monospace,monospace;font-size:.85rem}}
 .collisions{{background:#fff2f0;border-radius:.4rem;padding:.5rem .8rem}}
 .empty{{color:#999}}
</style></head>
<body>
<h1>uecs-mqtt webui</h1>
<div class="meta">
  broker: <code>{broker}:{broker_port}</code>
  · prefix: <code>{prefix}</code>
  · subscribed: <span id="subscribed">{subscribed}</span>
  · <span id="metrics">messages: —, topics: —</span>
</div>

<div id="content"
     hx-get="/partial" hx-trigger="load, every 2s" hx-swap="innerHTML">
  <p>loading…</p>
</div>

</body></html>
"""

_PARTIAL_TEMPLATE = """
<script>
document.getElementById("metrics").textContent = "messages: {total}, topics: {topic_count}";
</script>
{scopes_html}

<h2 style="font-size:1rem;margin:1.2rem 0 .4rem">recent collisions</h2>
{collisions_html}
"""


def render_index(broker: str, broker_port: int, prefix: str,
                 subscribe_started_at: float | None) -> str:
    subbed = (
        datetime.fromtimestamp(subscribe_started_at, _JST)
        .isoformat(timespec="seconds")
        if subscribe_started_at else "not yet"
    )
    return _INDEX_HTML.format(
        broker=broker, broker_port=broker_port,
        prefix=prefix, subscribed=subbed,
    )


def render_partial(state: State, prefix: str) -> str:
    with state._lock:
        entries = list(state.topics.values())
        collisions = list(state.collisions)[:20]
        total = state.total_messages
    entries.sort(key=lambda e: e.topic)
    grouped = group_topics(entries, prefix)
    now = time.time()

    scope_order = sorted(
        grouped.keys(),
        key=lambda s: (s == "_other", s),
    )
    scope_blocks: list[str] = []
    for scope in scope_order:
        cats = grouped[scope]
        cat_blocks: list[str] = []
        for cat in sorted(cats.keys()):
            rows: list[str] = []
            for e in cats[cat]:
                age_txt, cls = format_age(now, e.ts)
                val_str = _fmt_value(e.value, e.unit)
                type_seg = "/".join(e.topic.split("/")[3:])
                rows.append(
                    f'<tr><td><code>{_esc(type_seg)}</code></td>'
                    f'<td>{_esc(val_str)}</td>'
                    f'<td class="age {cls}">{age_txt}</td></tr>'
                )
            rows_html = (
                '<table><thead><tr><th>topic tail</th><th>value</th>'
                '<th>age</th></tr></thead><tbody>'
                + "".join(rows) + '</tbody></table>'
            )
            cat_blocks.append(
                f'<div class="cat"><h3>{_esc(cat)} ({len(cats[cat])})</h3>'
                f'{rows_html}</div>'
            )
        scope_blocks.append(
            f'<div class="scope"><h2>scope: {_esc(scope)} '
            f'({sum(len(v) for v in cats.values())} topics)</h2>'
            + "".join(cat_blocks) + '</div>'
        )
    scopes_html = "".join(scope_blocks) or '<p class="empty">no topics received yet</p>'

    if collisions:
        rows = "".join(
            f'<tr><td>{_esc(c["ts_iso"])}</td>'
            f'<td><code>{_esc(c["topic"])}</code></td>'
            f'<td>{_esc(c["prev_value"])}</td>'
            f'<td>{_esc(c["new_value"])}</td></tr>'
            for c in collisions
        )
        collisions_html = (
            '<div class="collisions"><table>'
            '<thead><tr><th>ts</th><th>topic</th><th>prev</th><th>new</th></tr>'
            '</thead><tbody>' + rows + '</tbody></table></div>'
        )
    else:
        collisions_html = '<p class="empty">(no collisions)</p>'

    return _PARTIAL_TEMPLATE.format(
        total=total,
        topic_count=len(entries),
        scopes_html=scopes_html,
        collisions_html=collisions_html,
    )


def _esc(s: Any) -> str:
    import html
    return html.escape(str(s), quote=True)


def _fmt_value(v: Any, unit: str) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        s = f"{v:.3g}"
    else:
        s = str(v)
    return f"{s} {unit}".strip()


# ══════════════════════════════════════════════
# MQTT subscriber (paho, 背景 thread)
# ══════════════════════════════════════════════

def start_mqtt_subscriber(
    state: State, broker: str, port: int, topic_prefix: str,
    client_id: str = "uecs-webui",
) -> Any:
    import paho.mqtt.client as mqtt

    def _on_connect(client, userdata, flags, reason_code, properties=None):
        client.subscribe(f"{topic_prefix}/#", qos=0)
        state.subscribe_started_at = time.time()
        logger.info("subscribed %s/#", topic_prefix)

    def _on_message(client, userdata, msg):
        try:
            state.on_message(msg.topic, msg.payload)
        except Exception as e:
            state.last_error = f"on_message: {e}"

    def _on_disconnect(client, userdata, *args):
        logger.warning("mqtt disconnected — auto-reconnect が動くはず")

    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=client_id,
    )
    client.on_connect = _on_connect
    client.on_message = _on_message
    client.on_disconnect = _on_disconnect
    client.connect_async(broker, port, keepalive=30)
    client.loop_start()
    return client


# ══════════════════════════════════════════════
# FastAPI app
# ══════════════════════════════════════════════

def build_app(state: State, broker: str, broker_port: int, prefix: str):
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse, JSONResponse

    app = FastAPI(title="uecs webui")

    @app.get("/", response_class=HTMLResponse)
    def index():
        return render_index(broker, broker_port, prefix,
                            state.subscribe_started_at)

    @app.get("/partial", response_class=HTMLResponse)
    def partial():
        return render_partial(state, prefix)

    @app.get("/health")
    def health():
        return JSONResponse({
            "subscribe_started_at": state.subscribe_started_at,
            "total_messages": state.total_messages,
            "topic_count": len(state.topics),
            "last_error": state.last_error,
        })

    return app


# ══════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="uecs_webui",
        description="Live MQTT topic viewer for uecs-mqtt-tools bridge.",
    )
    p.add_argument("--broker", required=True,
                   help="MQTT broker host (Pi4 の Tailscale IP など)")
    p.add_argument("--broker-port", type=int, default=1883)
    p.add_argument("--topic-prefix", default="agriha",
                   help="subscribe 対象 prefix (default: agriha)")
    p.add_argument("--listen", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8090)
    p.add_argument("--client-id", default="uecs-webui")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    state = State()
    _client = start_mqtt_subscriber(
        state, args.broker, args.broker_port, args.topic_prefix,
        client_id=args.client_id,
    )

    import uvicorn
    app = build_app(state, args.broker, args.broker_port, args.topic_prefix)
    uvicorn.run(app, host=args.listen, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
