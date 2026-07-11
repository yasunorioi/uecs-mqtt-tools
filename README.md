# uecs-mqtt-tools

UECS-CCM (UDP マルチキャスト) と MQTT の橋渡しをするツール群。
農業ハウスの UECS-CCM 機器 (ArsProut Pi, 自作 CCM ノード等) を MQTT ベースの
制御系 (Home Assistant, Node-RED, 自作 rule engine 等) に接続するための
一方向ブリッジと開発補助ツールを収録する。

## 収録物

### `bridges/`
- **`ccm_mqtt_bridge.py`** — CCM UDP マルチキャスト受信 → MQTT publish (一方向)
  - broker 同梱の `mosquitto_pub` を subprocess 呼びで使うので pip 依存は
    `pyyaml` のみ
  - トピック命名は `<prefix>/<scope>/<category>/<type>` (agriha 規約準拠、
    `topic_prefix` は設定可)
  - UECS `order` を尊重: order=1 は素のトピック、order>=2 は `/order` 付与
  - 同一トピックへ別発信元 (ip/room/region/order) が書こうとしたら WARN
- **`ccm-mqtt-bridge.service`** — systemd unit テンプレート

### `tools/`
- **`ccm_capture.py`** — UDP マルチキャストの passive listener。
  `<DATA>` を parse して jsonl 追記、`--type` / `--room` / `--region` / `--ip`
  フィルタで観察対象を絞れる。`inspect` サブコマンドで post-mortem 集計。
  - 参考仕様: [UARDECS](https://github.com/H-Kurosaki/UARDECS) (Arduino 版 UECS 実装)
  - **現地観測の癖**: **ArsProut 実装は 1 パケットに複数 DATA が入っていると
    取りこぼす**。extractor は `num_data > 1` の packet に `warn=multi_data_in_single_packet`
    を付ける — bridge を経由するデータが「なんか少ない」の初期切り分けに便利
- **`ccm_simulator.py`** — 合成 UECS-CCM パケット送出。実 CCM 機器が無い環境
  で bridge / capture を叩くのに使う。YAML シナリオで node/interval/value
  generator (`constant` / `ramp` / `sine` / `jitter`) を定義。default は
  1 packet = 1 DATA (ArsProut 互換)、`--multi-data-per-packet` で violation
  テストも可能。`examples/scenario.example.yaml` 参照

### `docs/`
- **`uecs-mqtt-bridge-generator.md`** — 元となった Node-RED フロー生成スキルの
  設計仕様

### `examples/`
- **`scope_map.example.yaml`** — `(room, region) → (scope, category)` 対応表
  および sender_override の設定例

## Quick start

```bash
# 1. 設定ファイルを準備
sudo mkdir -p /etc/uecs-mqtt-bridge
sudo cp examples/scope_map.example.yaml /etc/uecs-mqtt-bridge/config.yaml
sudo $EDITOR /etc/uecs-mqtt-bridge/config.yaml   # 現地の room/region に合わせる

# 2. ツール配置
sudo mkdir -p /opt/uecs-mqtt-tools/bridges
sudo cp bridges/ccm_mqtt_bridge.py /opt/uecs-mqtt-tools/bridges/

# 3. 依存
sudo apt install python3-yaml mosquitto-clients

# 4. 動作確認 (foreground)
python3 /opt/uecs-mqtt-tools/bridges/ccm_mqtt_bridge.py \
    --config /etc/uecs-mqtt-bridge/config.yaml

# 5. systemd 常駐化
sudo cp bridges/ccm-mqtt-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ccm-mqtt-bridge
sudo journalctl -u ccm-mqtt-bridge -f
```

## 予定 (TODO)

- **WebUI** — 稼働中の bridge の traffic を眺める + scope_map の live 検証

## 参考仕様

- [UARDECS (Arduino UECS)](https://github.com/H-Kurosaki/UARDECS) — CCM (UECS)
  の DATA XML パケットを送受する Arduino ライブラリ。実装 reference
- [Arsprout UECS-MQTT 変換ゲートウェイ仕様 v1.0](https://www.arsprout.co.jp/wp-content/uploads/2026/03/UECS-MQTT-GW-SPEC_1.0.pdf)
  (2026-03-23) — ArsProut 公式の MQTT-GW 仕様。本リポジトリの `bridges/` は
  この spec とは **異なる topic 体系** (`agriha/{scope}/{cat}/{type}` の 4-seg 圧縮版) を
  採用しており spec 完全準拠ではない。spec は `data/{app}/{env}/{user}/{room}/{region}/{order}/{type}`
  の 8-seg 構造 + request/response 型 topic + 変化量閾値 publish 抑制などを規定。
  将来 spec 互換 mode を追加する可能性あり (`docs/spec-compat.md` は現在未作成)

## 出自

`bridges/ccm_mqtt_bridge.py` は private repo `arsprout-analysis` の
`services/ccm_mqtt_bridge.py` (commit `7f9d68d`) から extract。
Node-RED 版仕様 (`docs/uecs-mqtt-bridge-generator.md`) の Python 実装。
