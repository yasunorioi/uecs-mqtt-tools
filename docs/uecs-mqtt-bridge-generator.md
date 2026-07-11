# UECS-MQTT ブリッジフロー生成スキル

UECS CCM（UDP マルチキャスト）とMQTTを相互変換するNode-REDフローを生成する。
既存のUECS機器（ArsproutPi等）と新規MQTT機器を共存させるためのブリッジ。

---

## 1. 概要

### 1.1 目的

UECS CCM プロトコル（UDP 224.0.0.1:16520）とMQTTブローカー間のデータ変換を行うNode-REDフローを自動生成する。

### 1.2 ユースケース

| シナリオ | 変換方向 | 説明 |
|---------|---------|------|
| MQTT機器からUECS制御 | MQTT → CCM | Pico W（MQTT）からArsproutPi（UECS）へ制御 |
| UECS機器のデータ収集 | CCM → MQTT | ArsproutPiのセンサーデータをMQTTで統合 |
| 新旧システム共存 | 双方向 | 段階的なMQTT移行期間中のブリッジ |

### 1.3 UECS CCM仕様

```
プロトコル: UDP マルチキャスト
アドレス: 224.0.0.1
ポート: 16520
形式: XML（UECS Ver1.00-E10準拠）
```

---

## 2. 使用方法

### 2.1 入力パラメータ

```yaml
# ブリッジ設定
bridge:
  direction: "bidirectional"  # ccm_to_mqtt / mqtt_to_ccm / bidirectional

# MQTT設定
mqtt:
  broker: "localhost"
  port: 1883
  username: null              # 認証なしの場合null
  password: null
  topic_prefix: "uecs"        # トピック: uecs/{type}/{room}/{region}/{order}

# UECS設定
uecs:
  multicast_address: "224.0.0.1"
  port: 16520
  version: "1.00-E10"

# フィルタ設定（オプション）
filter:
  room: [1, 2]                # 対象room（空=全て）
  region: [11, 61]            # 対象region（11=内気象, 61=制御）
  types:                      # 対象CCM識別子
    - "InAirTemp"
    - "InAirHumid"
    - "Irriopr"
    - "IrrircA"

# ノードデフォルト値
defaults:
  room: 1
  region: 1
  priority: 29
```

### 2.2 パラメータ詳細

| パラメータ | 型 | 必須 | 説明 |
|-----------|-----|------|------|
| bridge.direction | string | Yes | 変換方向 |
| mqtt.broker | string | Yes | MQTTブローカーホスト |
| mqtt.port | int | Yes | MQTTポート（通常1883） |
| mqtt.topic_prefix | string | Yes | MQTTトピックプレフィックス |
| uecs.multicast_address | string | Yes | CCMマルチキャストアドレス |
| uecs.port | int | Yes | CCMポート（16520固定） |
| filter.room | array | No | 対象roomフィルタ |
| filter.region | array | No | 対象regionフィルタ |
| filter.types | array | No | 対象CCM識別子フィルタ |

---

## 3. 出力形式

### 3.1 Node-REDフローJSON

生成されるフローには以下のノードが含まれる：

| ノード | CCM→MQTT | MQTT→CCM | 説明 |
|--------|----------|----------|------|
| udp-in | ○ | - | CCM受信 |
| xml parse | ○ | - | XML解析 |
| function (ccm2mqtt) | ○ | - | CCM→MQTT変換 |
| mqtt-out | ○ | - | MQTT送信 |
| mqtt-in | - | ○ | MQTT受信 |
| function (mqtt2ccm) | - | ○ | MQTT→CCM変換 |
| udp-out | - | ○ | CCM送信 |

---

## 4. CCM→MQTT 変換フロー

### 4.1 フロー構成

```
[udp-in]──→[xml]──→[function]──→[switch]──→[mqtt-out]
 224.0.0.1   parse    ccm2mqtt     filter
 :16520
```

### 4.2 Node-REDフローJSON

```json
[
    {
        "id": "uecs_udp_in",
        "type": "udp in",
        "name": "UECS CCM受信",
        "iface": "",
        "port": "16520",
        "ipv": "udp4",
        "multicast": "true",
        "group": "224.0.0.1",
        "datatype": "utf8",
        "wires": [["uecs_xml_parse"]]
    },
    {
        "id": "uecs_xml_parse",
        "type": "xml",
        "name": "XML解析",
        "property": "payload",
        "attr": "$",
        "chr": "_",
        "wires": [["uecs_ccm2mqtt"]]
    },
    {
        "id": "uecs_ccm2mqtt",
        "type": "function",
        "name": "CCM→MQTT変換",
        "func": "// CCM XMLをMQTT JSONに変換\nconst uecs = msg.payload.UECS;\nif (!uecs || !uecs.DATA) {\n    node.warn('Invalid CCM format');\n    return null;\n}\n\nconst data = uecs.DATA[0];\nconst attrs = data.$;\n\n// MQTTトピック生成\nconst topic = `uecs/${attrs.type}/${attrs.room}/${attrs.region}/${attrs.order}`;\n\n// MQTT ペイロード生成\nmsg.topic = topic;\nmsg.payload = {\n    type: attrs.type,\n    room: parseInt(attrs.room),\n    region: parseInt(attrs.region),\n    order: parseInt(attrs.order),\n    priority: parseInt(attrs.priority),\n    value: parseFloat(data._) || data._,\n    timestamp: new Date().toISOString()\n};\n\nreturn msg;",
        "outputs": 1,
        "wires": [["uecs_filter", "uecs_mqtt_out"]]
    },
    {
        "id": "uecs_filter",
        "type": "switch",
        "name": "フィルタ",
        "property": "payload.type",
        "propertyType": "msg",
        "rules": [
            {"t": "regex", "v": "InAirTemp|InAirHumid|InAirCO2", "vt": "str"},
            {"t": "regex", "v": "Irri.*|Valve.*", "vt": "str"},
            {"t": "else"}
        ],
        "checkall": "false",
        "repair": false,
        "outputs": 3,
        "wires": [["uecs_mqtt_out"], ["uecs_mqtt_out"], []]
    },
    {
        "id": "uecs_mqtt_out",
        "type": "mqtt out",
        "name": "MQTT送信",
        "topic": "",
        "qos": "1",
        "retain": "false",
        "broker": "mqtt_broker",
        "wires": []
    },
    {
        "id": "mqtt_broker",
        "type": "mqtt-broker",
        "name": "Local Mosquitto",
        "broker": "localhost",
        "port": "1883",
        "clientid": "nodered-uecs-bridge",
        "usetls": false,
        "compatmode": false,
        "keepalive": "60",
        "cleansession": true
    }
]
```

### 4.3 CCM→MQTT 変換ロジック詳細

```javascript
// CCM XMLをMQTT JSONに変換
// 入力: XML形式のCCMパケット
// 出力: JSON形式のMQTTメッセージ

const uecs = msg.payload.UECS;
if (!uecs || !uecs.DATA) {
    node.warn('Invalid CCM format');
    return null;
}

const data = uecs.DATA[0];
const attrs = data.$;

// MQTTトピック生成
// 形式: uecs/{type}/{room}/{region}/{order}
const topic = `uecs/${attrs.type}/${attrs.room}/${attrs.region}/${attrs.order}`;

// MQTT ペイロード生成
msg.topic = topic;
msg.payload = {
    type: attrs.type,
    room: parseInt(attrs.room),
    region: parseInt(attrs.region),
    order: parseInt(attrs.order),
    priority: parseInt(attrs.priority),
    value: parseFloat(data._) || data._,
    timestamp: new Date().toISOString()
};

return msg;
```

---

## 5. MQTT→CCM 変換フロー

### 5.1 フロー構成

```
[mqtt-in]──→[function]──→[udp-out]
 uecs/#      mqtt2ccm    224.0.0.1
                         :16520
```

### 5.2 Node-REDフローJSON

```json
[
    {
        "id": "mqtt_uecs_in",
        "type": "mqtt in",
        "name": "MQTT購読",
        "topic": "uecs/+/+/+/+",
        "qos": "1",
        "datatype": "json",
        "broker": "mqtt_broker",
        "wires": [["mqtt2ccm"]]
    },
    {
        "id": "mqtt2ccm",
        "type": "function",
        "name": "MQTT→CCM変換",
        "func": "// MQTT JSONをCCM XMLに変換\nconst topicParts = msg.topic.split('/');\nif (topicParts.length < 5) {\n    node.warn('Invalid topic format: ' + msg.topic);\n    return null;\n}\n\n// トピックから属性取得\n// 形式: uecs/{type}/{room}/{region}/{order}\nconst type = topicParts[1];\nconst room = topicParts[2];\nconst region = topicParts[3];\nconst order = topicParts[4];\n\n// ペイロードから値取得\nlet value = msg.payload;\nif (typeof value === 'object') {\n    value = msg.payload.value;\n}\n\n// 優先度（デフォルト29）\nconst priority = msg.payload.priority || 29;\n\n// CCM XML生成\nmsg.payload = `<?xml version=\"1.0\"?>\\n<UECS ver=\"1.00-E10\">\\n<DATA type=\"${type}\" room=\"${room}\" region=\"${region}\" order=\"${order}\" priority=\"${priority}\">${value}</DATA>\\n</UECS>`;\n\nreturn msg;",
        "outputs": 1,
        "wires": [["uecs_udp_out"]]
    },
    {
        "id": "uecs_udp_out",
        "type": "udp out",
        "name": "CCM送信",
        "addr": "224.0.0.1",
        "iface": "",
        "port": "16520",
        "ipv": "udp4",
        "outport": "",
        "base64": false,
        "multicast": "true",
        "wires": []
    }
]
```

### 5.3 MQTT→CCM 変換ロジック詳細

```javascript
// MQTT JSONをCCM XMLに変換
// 入力: JSON形式のMQTTメッセージ
// 出力: XML形式のCCMパケット

const topicParts = msg.topic.split('/');
if (topicParts.length < 5) {
    node.warn('Invalid topic format: ' + msg.topic);
    return null;
}

// トピックから属性取得
// 形式: uecs/{type}/{room}/{region}/{order}
const type = topicParts[1];
const room = topicParts[2];
const region = topicParts[3];
const order = topicParts[4];

// ペイロードから値取得
let value = msg.payload;
if (typeof value === 'object') {
    value = msg.payload.value;
}

// 優先度（デフォルト29）
const priority = msg.payload.priority || 29;

// CCM XML生成
msg.payload = `<?xml version="1.0"?>
<UECS ver="1.00-E10">
<DATA type="${type}" room="${room}" region="${region}" order="${order}" priority="${priority}">${value}</DATA>
</UECS>`;

return msg;
```

---

## 6. フィルタリング機能

### 6.1 room/region フィルタ

特定のroom（ハウス番号）やregion（系統番号）のみを対象にする。

```javascript
// フィルタ設定
const allowedRooms = [1, 2];      // 対象room
const allowedRegions = [11, 61];  // 11=内気象, 61=制御

// フィルタ判定
const room = parseInt(msg.payload.room);
const region = parseInt(msg.payload.region);

if (allowedRooms.length > 0 && !allowedRooms.includes(room)) {
    return null;  // フィルタ除外
}

if (allowedRegions.length > 0 && !allowedRegions.includes(region)) {
    return null;  // フィルタ除外
}

return msg;
```

### 6.2 type フィルタ

特定のCCM識別子のみを対象にする。

```javascript
// 許可するCCM識別子
const allowedTypes = [
    "InAirTemp",    // 室内気温
    "InAirHumid",   // 室内湿度
    "InAirCO2",     // 室内CO2
    "Irriopr",      // 灌水状態
    "IrrircA",      // 灌水制御指示
    "Valveopr",     // バルブ状態
    "ValvercA"      // バルブ制御指示
];

const type = msg.payload.type;

if (allowedTypes.length > 0 && !allowedTypes.includes(type)) {
    return null;  // フィルタ除外
}

return msg;
```

### 6.3 switchノードによるフィルタ設定

```json
{
    "id": "uecs_type_filter",
    "type": "switch",
    "name": "CCMタイプフィルタ",
    "property": "payload.type",
    "propertyType": "msg",
    "rules": [
        {"t": "regex", "v": "^InAir(Temp|Humid|CO2)$", "vt": "str"},
        {"t": "regex", "v": "^(Irri|Valve)(opr|rcA|rcM)$", "vt": "str"},
        {"t": "regex", "v": "^W(AirTemp|RainfallAmt)$", "vt": "str"}
    ],
    "checkall": "false",
    "outputs": 3
}
```

---

## 7. MQTTトピック設計

### 7.1 標準トピック形式

```
uecs/{type}/{room}/{region}/{order}
```

| セグメント | 説明 | 例 |
|-----------|------|-----|
| uecs | 固定プレフィックス | - |
| {type} | CCM識別子 | InAirTemp, Irriopr |
| {room} | ハウス番号 | 1, 2, 3 |
| {region} | 系統番号 | 11（内気象）, 61（制御） |
| {order} | ノード番号 | 1, 2, 3 |

### 7.2 トピック例

| トピック | 意味 |
|---------|------|
| `uecs/InAirTemp/1/11/1` | ハウス1, 内気象系統, ノード1の室内気温 |
| `uecs/IrrircA/1/61/2` | ハウス1, 制御系統, ノード2の灌水制御指示 |
| `uecs/Valveopr/2/61/1` | ハウス2, 制御系統, ノード1のバルブ状態 |

### 7.3 ワイルドカード購読

```
uecs/+/+/+/+     # 全CCM
uecs/InAir+/+/+/+  # 室内気象系
uecs/+/1/+/+     # ハウス1のみ
uecs/+/+/61/+    # 制御系統のみ
```

---

## 8. 完全な双方向ブリッジフロー

### 8.1 フロー構成図

```
┌─────────────────────────────────────────────────────────────┐
│                    Node-RED Bridge                           │
│                                                              │
│  ┌──────────┐   ┌──────────┐   ┌──────────┐                 │
│  │ udp-in   │──→│ xml      │──→│ function │──→┌──────────┐  │
│  │ (CCM受信) │   │ (parse)  │   │(ccm2mqtt)│   │ mqtt-out │  │
│  └──────────┘   └──────────┘   └──────────┘   └──────────┘  │
│                                                              │
│  ┌──────────┐   ┌──────────┐   ┌──────────┐                 │
│  │ udp-out  │←──│ function │←──│ mqtt-in  │                 │
│  │ (CCM送信) │   │(mqtt2ccm)│   │ (購読)   │                 │
│  └──────────┘   └──────────┘   └──────────┘                 │
│                                                              │
└─────────────────────────────────────────────────────────────┘
       ↑                                    ↑
       │ UDP 224.0.0.1:16520               │ MQTT localhost:1883
       ↓                                    ↓
┌──────────────┐                    ┌──────────────┐
│  ArsproutPi  │                    │  Pico W      │
│  (UECS機器)   │                    │  (MQTT機器)  │
└──────────────┘                    └──────────────┘
```

### 8.2 完全フローJSON

```json
[
    {
        "id": "flow_uecs_bridge",
        "type": "tab",
        "label": "UECS-MQTT Bridge",
        "disabled": false
    },
    {
        "id": "comment_header",
        "type": "comment",
        "z": "flow_uecs_bridge",
        "name": "UECS-MQTT 双方向ブリッジ",
        "info": "CCM (UDP 224.0.0.1:16520) ⇔ MQTT (localhost:1883)",
        "x": 160,
        "y": 40
    },
    {
        "id": "uecs_udp_in",
        "type": "udp in",
        "z": "flow_uecs_bridge",
        "name": "CCM受信",
        "iface": "",
        "port": "16520",
        "ipv": "udp4",
        "multicast": "true",
        "group": "224.0.0.1",
        "datatype": "utf8",
        "x": 100,
        "y": 120,
        "wires": [["uecs_xml_parse"]]
    },
    {
        "id": "uecs_xml_parse",
        "type": "xml",
        "z": "flow_uecs_bridge",
        "name": "XML解析",
        "property": "payload",
        "attr": "$",
        "chr": "_",
        "x": 260,
        "y": 120,
        "wires": [["uecs_ccm2mqtt"]]
    },
    {
        "id": "uecs_ccm2mqtt",
        "type": "function",
        "z": "flow_uecs_bridge",
        "name": "CCM→MQTT",
        "func": "const uecs = msg.payload.UECS;\nif (!uecs || !uecs.DATA) {\n    return null;\n}\n\nconst data = uecs.DATA[0];\nconst attrs = data.$;\n\nmsg.topic = `uecs/${attrs.type}/${attrs.room}/${attrs.region}/${attrs.order}`;\nmsg.payload = {\n    type: attrs.type,\n    room: parseInt(attrs.room),\n    region: parseInt(attrs.region),\n    order: parseInt(attrs.order),\n    priority: parseInt(attrs.priority),\n    value: parseFloat(data._) || data._,\n    timestamp: new Date().toISOString()\n};\n\nreturn msg;",
        "outputs": 1,
        "x": 430,
        "y": 120,
        "wires": [["uecs_mqtt_out"]]
    },
    {
        "id": "uecs_mqtt_out",
        "type": "mqtt out",
        "z": "flow_uecs_bridge",
        "name": "MQTT送信",
        "topic": "",
        "qos": "1",
        "retain": "false",
        "broker": "mqtt_broker_local",
        "x": 610,
        "y": 120,
        "wires": []
    },
    {
        "id": "mqtt_uecs_in",
        "type": "mqtt in",
        "z": "flow_uecs_bridge",
        "name": "MQTT購読",
        "topic": "uecs/+/+/+/+",
        "qos": "1",
        "datatype": "json",
        "broker": "mqtt_broker_local",
        "x": 100,
        "y": 220,
        "wires": [["mqtt2ccm"]]
    },
    {
        "id": "mqtt2ccm",
        "type": "function",
        "z": "flow_uecs_bridge",
        "name": "MQTT→CCM",
        "func": "const topicParts = msg.topic.split('/');\nif (topicParts.length < 5) {\n    return null;\n}\n\nconst type = topicParts[1];\nconst room = topicParts[2];\nconst region = topicParts[3];\nconst order = topicParts[4];\n\nlet value = msg.payload;\nif (typeof value === 'object') {\n    value = msg.payload.value;\n}\n\nconst priority = msg.payload.priority || 29;\n\nmsg.payload = `<?xml version=\"1.0\"?>\\n<UECS ver=\"1.00-E10\">\\n<DATA type=\"${type}\" room=\"${room}\" region=\"${region}\" order=\"${order}\" priority=\"${priority}\">${value}</DATA>\\n</UECS>`;\n\nreturn msg;",
        "outputs": 1,
        "x": 290,
        "y": 220,
        "wires": [["uecs_udp_out"]]
    },
    {
        "id": "uecs_udp_out",
        "type": "udp out",
        "z": "flow_uecs_bridge",
        "name": "CCM送信",
        "addr": "224.0.0.1",
        "iface": "",
        "port": "16520",
        "ipv": "udp4",
        "outport": "",
        "base64": false,
        "multicast": "true",
        "x": 470,
        "y": 220,
        "wires": []
    },
    {
        "id": "mqtt_broker_local",
        "type": "mqtt-broker",
        "name": "Local Mosquitto",
        "broker": "localhost",
        "port": "1883",
        "clientid": "nodered-uecs-bridge",
        "usetls": false,
        "keepalive": "60",
        "cleansession": true
    }
]
```

---

## 9. CCM識別子一覧

### 9.1 センサー系（region=11）

| CCM識別子 | 名称 | 単位 | 優先度 |
|-----------|------|------|--------|
| InAirTemp | 室内気温 | °C | 1 |
| InAirHumid | 室内湿度 | % | 1 |
| InAirCO2 | 室内CO2 | ppm | 1 |
| WAirTemp | 屋外気温 | °C | 1 |
| WRainfallAmt | 降水量 | mm | 29 |
| SoilTemp | 土壌温度 | °C | 1 |
| SoilWC | 土壌水分 | % | 1 |

### 9.2 アクチュエータ系（region=61）

| CCM識別子 | 名称 | 値 | 用途 |
|-----------|------|-----|------|
| Irriopr | 灌水状態 | 0/1 | 状態通知 |
| IrrircA | 灌水制御（自動） | 0/1 | 制御指示 |
| IrrircM | 灌水制御（手動） | 0/1 | 手動操作 |
| Valveopr | バルブ状態 | 0/1 | 状態通知 |
| ValvercA | バルブ制御（自動） | 0/1 | 制御指示 |
| VenFanopr | 換気扇状態 | 0/1 | 状態通知 |

---

## 10. 注意事項

### 10.1 UDPマルチキャストの制限

- Docker環境では `--network host` が必要
- 仮想環境では追加設定が必要な場合あり
- ファイアウォールでUDP 16520を許可

### 10.2 IGMP タイムアウト

- IGMPアクティブなスイッチでは4分でタイムアウト
- 対策: IGMPジョイン再送の実装

```javascript
// 3分ごとにIGMPジョイン再送
setInterval(() => {
    // Node-REDのudp-inノードは自動再参加しない場合がある
    // 必要に応じてフロー再デプロイ
}, 180000);
```

### 10.3 ループ防止

双方向ブリッジではメッセージのループに注意。

```javascript
// ループ防止: ブリッジ経由のメッセージにフラグを付与
if (msg._bridged) {
    return null;  // 既にブリッジ経由
}
msg._bridged = true;
```

---

## 11. 参考資料

- UECS実用通信規約 Ver1.00-E10: https://uecs.jp/
- Node-RED UDP nodes: https://nodered.org/docs/user-guide/nodes
- Node-RED XML node: https://flows.nodered.org/node/node-red-node-xml
- Mosquitto MQTT: https://mosquitto.org/

---

**作成日**: 2026-02-04
**作成者**: 足軽1号
**parent_cmd**: cmd_027
