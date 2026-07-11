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

- **sensor simulator** — 実 CCM 機器なしで合成 UDP パケットを流して bridge を叩く
- **WebUI** — 稼働中の bridge の traffic を眺める + scope_map の live 検証

## 出自

`bridges/ccm_mqtt_bridge.py` は private repo `arsprout-analysis` の
`services/ccm_mqtt_bridge.py` (commit `7f9d68d`) から extract。
Node-RED 版仕様 (`docs/uecs-mqtt-bridge-generator.md`) の Python 実装。
