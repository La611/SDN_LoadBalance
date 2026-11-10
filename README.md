# SDN 負載平衡與 NAT 控制器

本專案使用 **Ryu Controller** 與 **OpenFlow
1.3**，實作一套結合動態路徑選擇、伺服器負載平衡、NAT，以及交換機彈性擴縮的
SDN 控制程式。

這份 README 以程式碼 `SDNLoadBalancerNAT`
為依據，說明程式架構、主要資料結構、封包處理流程、負載計算方式、設定參數與測試時需要注意的事項，讓後續維護者可以先理解整體設計，再進入各個函式閱讀。

------------------------------------------------------------------------

## 1. 系統功能概覽

程式主要包含以下功能：

1.  **集中式控制**：由 Ryu Controller 管理 OpenFlow 交換機，接收
    Packet-In 與交換機統計資料。
2.  **動態路徑選擇**：Client 對 VIP 發起新的 TCP
    連線時，從目前啟用的中間交換機中選擇負載較低的路徑。
3.  **彈性擴縮（Elastic Scaling）**：依據 Client
    入口流量，動態啟用或停用中間層交換機。
4.  **伺服器負載平衡**：根據伺服器目前的 TCP
    連線數與流量速率，選擇負載分數最低的伺服器。
5.  **NAT**：將 Client 連線目的地 VIP 轉換成實際伺服器
    IP（DNAT），並將伺服器回應來源 IP 改回 VIP（SNAT）。
6.  **統計資料 API**：透過 REST API
    提供鏈路流量、伺服器負載與啟用中的中間交換機資訊，供外部監控介面讀取。

------------------------------------------------------------------------

## 2. 系統架構與預期拓樸

程式內使用 6 台交換機，DPID 對應如下：

  DPID   程式中的角色   主要用途
  ------ -------------- --------------------------------
  1      S1             Client H1 的入口交換機
  2      S2             Client H2 的入口交換機
  3      S3             中間轉發路徑 1
  4      S4             中間轉發路徑 2
  5      S5             中間轉發路徑 3
  6      S6             出口交換機，執行負載平衡與 NAT

### 2.1 依程式設定推導的連接關係

程式在 `setup_topology_graph()` 中定義了以下交換機連線及連接埠：

  起點     起點 Port 終點     終點 Port
  ------ ----------- ------ -----------
  S1               2 S3               1
  S1               3 S4               1
  S1               4 S5               1
  S2               2 S3               2
  S2               3 S4               2
  S2               4 S5               2
  S3               3 S6               1
  S4               3 S6               2
  S5               3 S6               3

另外，程式假設： - S1 Port 1 連接 Client H1。 - S2 Port 1 連接 Client
H2。 - S6 Port 4 連接 Server H3。 - S6 Port 5 連接 Server H4。

> **重要：** 上表是依照程式中的 DPID、Port
> 與轉發邏輯整理出的預期拓樸。實際 Mininet
> 拓樸必須使用相同的連接埠配置，否則封包可能被送往錯誤介面。`setup_topology_graph()`
> 建立的是 NetworkX 圖形資料，並不會自動建立 Mininet 連線。

### 2.2 IP 與 MAC 設定

  節點        IP               MAC / 說明
  ----------- ---------------- ---------------------------
  Client H1   `172.16.0.1`     Client MAC 由封包動態學習
  Client H2   `172.16.0.2`     Client MAC 由封包動態學習
  VIP         `172.16.0.254`   `00:00:00:00:02:54`
  Server H3   `192.168.0.10`   `00:00:00:00:00:03`
  Server H4   `192.168.0.20`   `00:00:00:00:00:04`

Client 連線時應以 VIP `172.16.0.254` 作為服務目的 IP，程式目前只針對目的
TCP Port `80` 的連線執行伺服器分配與 NAT。

------------------------------------------------------------------------

## 3. 程式執行架構

主要類別為：

``` python
class SDNLoadBalancerNAT(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
```

此類別繼承 Ryu 的
`RyuApp`，負責交換機事件處理、流表安裝、流量統計、路徑選擇、負載平衡與
NAT。

程式啟動後，大致依照以下順序運作：

1.  初始化交換機、伺服器、連線與統計資料。
2.  建立 NetworkX 拓樸圖。
3.  啟動背景監控執行緒 `_monitor()`。
4.  交換機連線後，透過 `switch_features_handler()` 安裝 Table-Miss Flow
    Entry。
5.  交換機回報 Port Statistics 與 Flow Statistics。
6.  Client 封包進入時，由 `_packet_in_handler()`
    判斷封包種類與目的地，執行對應轉發或 NAT。
7.  控制器依統計結果更新路徑與伺服器負載資訊。

------------------------------------------------------------------------

## 4. 重要參數與資料結構

### 4.1 負載平衡權重

``` python
CONN_WEIGHT = 0.6
TRAFFIC_WEIGHT = 0.4

MAX_EXPECTED_CONNECTIONS = 100.0
MAX_EXPECTED_TRAFFIC_KBPS = 10 * 1024.0
```

伺服器負載分數由連線數與流量兩部分組成： - 連線數權重：0.6。 -
流量權重：0.4。 - 預設滿載參考值：100 條連線、10 × 1024 KB/s 流量。

這些參數是負載分數的正規化基準，不代表伺服器的實際硬體上限。

### 4.2 主要資料結構

  --------------------------------------------------------------------------------------
  變數                                用途
  ----------------------------------- --------------------------------------------------
  `self.datapaths`                    儲存已連線交換機的 Datapath 物件，以 DPID 為索引

  `self.net`                          NetworkX 有向拓樸圖

  `self.SERVERS`                      儲存伺服器 IP、MAC、連接埠、流量、連線數與負載分數

  `self.connections`                  記錄 Client TCP 連線與其分配到的伺服器

  `self.client_mac_table`             記錄 Client IP 對應的 MAC Address

  `self.port_stats`                   儲存各交換機連接埠的前次計數器、時間與計算後速率

  `self.link_rates`                   鏈路速率相關資料的預留欄位；目前主要統計使用
                                      `port_stats`

  `self.active_mid_switches`          目前啟用的中間交換機清單

  `self.mid_active_conns`             各中間交換機目前被分配的連線數

  `self.flow_to_mid_map`              記錄 TCP Flow 被分配到哪一台中間交換機
  --------------------------------------------------------------------------------------

------------------------------------------------------------------------

## 5. 中間交換機的動態擴縮

### 5.1 設計目的

中間層有 S3、S4、S5 三條可選路徑。程式初始只啟用 S3，當 Client
入口總流量增加時，再依門檻逐步啟用
S4、S5；流量下降時則逐步縮減啟用數量。

``` python
self.active_mid_switches = [3]

self.SCALE_UP_THRESHOLDS = {
    1: 2000.0,
    2: 5000.0,
}

self.SCALE_DOWN_THRESHOLDS = {
    3: 4000.0,
    2: 1200.0,
}
```

流量單位為 KB/s。

### 5.2 流量來源與計算

`port_stats_reply_handler()` 取得 S1 Port 1 與 S2 Port 1 的速率：

``` python
r_c1 = self.port_stats.get((1, 1), {}).get('rate', 0.0)
r_c2 = self.port_stats.get((2, 1), {}).get('rate', 0.0)

self.current_mid_traffic_kbps = r_c1 + r_c2
```

也就是將兩個 Client 入口的流量相加，作為中間層擴縮判斷依據。

### 5.3 擴容與縮容規則

    目前啟用數量 判斷條件            調整結果
  -------------- ------------------- ----------------------
            1 台 流量 \> 2000 KB/s   啟用 S3、S4
            2 台 流量 \> 5000 KB/s   啟用 S3、S4、S5
            3 台 流量 \< 4000 KB/s   關閉 S5，保留 S3、S4
            2 台 流量 \< 1200 KB/s   關閉 S4，只保留 S3

上述判斷由 `check_and_scale_mid_switches(traffic_rate)`
執行。當啟用清單改變時，程式會輸出 `[ELASTIC SCALING]` 訊息。

**既有連線處理方式：**
程式註解指出，擴縮時不主動中斷既有連線，新的連線才會依照更新後的啟用清單選擇路徑。這有助於避免因擴縮而直接切斷正在傳輸的連線。

------------------------------------------------------------------------

## 6. 中間路徑選擇機制

主要函式：

``` python
get_least_loaded_mid_port(dpid, src_ip, src_port)
```

當 S1 或 S2 收到 Client 前往 VIP 的 TCP 封包時，程式會呼叫此函式。

### 6.1 可用交換機與連接埠對應

``` python
dpid_to_port = {
    3: 2,
    4: 3,
    5: 4
}
```

這是入口交換機 S1、S2 對應到 S3、S4、S5 的輸出 Port。

### 6.2 路徑負載評估

當可用路徑多於一條時，程式使用以下方式計算路徑負載：

``` python
effective_load = rate + conns * 500.0
```

其中： - `rate`：入口交換機對應 Port 的即時流量速率，單位 KB/s。 -
`conns`：該中間交換機目前被記錄的活躍連線數。 -
`500.0`：每條連線在此負載公式中的加權值。

程式以 `min()` 選出 `effective_load`
最小的中間交換機。若負載相同，Python 的 `min()`
會依照迭代順序選出第一個最小值。

選定路徑後，程式會將 `(src_ip, src_port)` 與中間交換機編號記錄在
`flow_to_mid_map`，並增加該交換機的連線計數。

> 注意：此處的 `rate` 取自 S1 或 S2 對應的入口 Port
> 統計值，並非直接量測整台中間交換機的總負載。`conns * 500`
> 則是程式自行設定的負載權重，不是交換機實際 CPU 或頻寬使用率。

------------------------------------------------------------------------

## 7. 伺服器負載平衡機制

### 7.1 伺服器資訊

程式目前設定兩台伺服器：

  伺服器   IP                 S6 輸出 Port
  -------- ---------------- --------------
  H3       `192.168.0.10`                4
  H4       `192.168.0.20`                5

伺服器的負載資訊包含： - `conns`：目前記錄的 TCP 連線數。 - `rate`：S6
對應伺服器 Port 的流量速率。 - `conn_score`：連線數正規化分數。 -
`traffic_score`：流量正規化分數。 - `load_score`：最後的綜合負載分數。

### 7.2 負載分數計算

由 `update_load_scores()` 執行：

``` python
conn_score = min(conns / MAX_EXPECTED_CONNECTIONS, 1.0)
traffic_score = min(rate / MAX_EXPECTED_TRAFFIC_KBPS, 1.0)

load_score = (
    CONN_WEIGHT * conn_score
    + TRAFFIC_WEIGHT * traffic_score
)
```

公式可表示為：

\[ L = 0.6 `\times `{=tex}C + 0.4 `\times `{=tex}T \]

其中：

\[ C = `\min`{=tex}`\left`{=tex}(`\frac{\text{目前連線數}}{100}`{=tex},
1`\right`{=tex}) \]

\[ T =
`\min`{=tex}`\left`{=tex}(`\frac{\text{目前流量 KB/s}}{10240}`{=tex},
1`\right`{=tex}) \]

負載分數越低，代表依照目前設定的指標估算，其負載越低。

### 7.3 選擇伺服器

``` python
best_server = min(
    self.SERVERS.items(),
    key=lambda item: item[1]['load_score']
)
```

`get_least_loaded_server()` 會選出 `load_score` 最低的伺服器，作為新 TCP
連線的目的伺服器。

### 7.4 連線追蹤

S6 收到目的地為 VIP、TCP Port 80 的封包時： 1. 先使用 Client IP、Client
TCP Source Port、VIP 與目的 Port 組成連線識別鍵。 2.
若連線已存在，沿用原本分配的伺服器。 3. 若是新連線，檢查封包是否為 TCP
SYN 且不是 ACK。 4. 符合條件時，呼叫 `get_least_loaded_server()`。 5.
將連線與伺服器的對應關係記錄在 `self.connections`。 6. 安裝對應的 DNAT
與 SNAT 流表。

這種作法可讓同一條 TCP
連線持續使用原先分配的伺服器，而不是每個封包都重新選擇。

------------------------------------------------------------------------

## 8. NAT 與 OpenFlow 流表

NAT 主要在 S6 執行，分成 DNAT 與 SNAT 兩個方向。

### 8.1 DNAT：Client 前往 Server

Client 連線目的地為：

``` text
172.16.0.254:80
```

控制器選定伺服器後，將封包欄位修改為： - Ethernet Source MAC：VIP
MAC。 - Ethernet Destination MAC：實際伺服器 MAC。 - IPv4
Destination：實際伺服器 IP。 - Output Port：伺服器所連接的 S6 Port。

例如，若分配到 H3：

``` text
原目的 IP：172.16.0.254
轉換後：  192.168.0.10
```

### 8.2 SNAT：Server 回覆 Client

伺服器回應時，程式將： - Ethernet Source MAC 設為 VIP MAC。 - Ethernet
Destination MAC 設為 Client MAC。 - IPv4 Source 設為 VIP
`172.16.0.254`。 - Output Port 設為 Client 對應的出口。

例如：

``` text
原來源 IP：192.168.0.10
轉換後：  172.16.0.254
```

如此一來，Client 看到的回應來源仍是 VIP，而不是實際伺服器 IP。

### 8.3 流表安裝

`install_tcp_connection_flows()` 會安裝兩條主要規則：

  --------------------------------------------------------------------------
  規則                    Match 主要條件             Action
  ----------------------- -------------------------- -----------------------
  DNAT                    Client                     修改目的 IP / MAC，送往
                          IP、VIP、TCP、來源與目的   Server
                          Port                       

  SNAT                    Server IP、Client          修改來源 IP / MAC，送往
                          IP、TCP、來源與目的 Port   Client
  --------------------------------------------------------------------------

兩條規則的優先權為 200，Idle Timeout 使用
`CONNECTION_IDLE_TIMEOUT`（預設 300 秒）。

流表安裝後，後續符合 Match
條件的封包可直接由交換機轉發，不需要每個封包都送到 Controller。

------------------------------------------------------------------------

## 9. 封包處理流程

主要封包處理函式為：

``` python
_packet_in_handler(self, ev)
```

它會解析 Ethernet、ARP、IPv4 與 TCP 標頭，再依照 DPID
與封包目的地決定處理方式。

### 9.1 ARP 封包

-   Client 查詢 VIP `172.16.0.254` 或 `192.168.0.254` 時，程式透過
    `handle_arp_reply()` 回覆虛擬 MAC。
-   其他 ARP 封包則依照目的 IP 與目前交換機角色，選擇對應輸出 Port。
-   S6 會依伺服器 IP 將 ARP 封包送往 H3 或 H4。

### 9.2 IPv4 封包

  -----------------------------------------------------------------------
  交換機                              處理方式
  ----------------------------------- -----------------------------------
  S1、S2                              Client 前往 VIP
                                      時選擇中間路徑；回程封包則送往
                                      Client

  S3、S4、S5                          依目的 Client IP 將封包轉往 S1 或
                                      S2；其他目的地依程式設定送往 Port 3

  S6                                  Client 前往 VIP 的 TCP/80
                                      連線執行伺服器選擇與 DNAT；Server
                                      回應執行 SNAT
  -----------------------------------------------------------------------

### 9.3 Table-Miss Flow Entry

`switch_features_handler()` 在交換機連線後安裝優先權為 0 的 Table-Miss
Flow Entry，將未符合其他流表規則的封包送往 Controller。

當 Controller 判斷封包應如何處理後，會透過 `add_flow()`
安裝新的流表，並使用 Packet-Out 處理觸發該次 Packet-In 的封包。

------------------------------------------------------------------------

## 10. 流量監控與統計

### 10.1 背景監控

`_monitor()` 每隔 2 秒向已連線的交換機發送 Port Statistics Request：

``` python
hub.sleep(2)
```

此外，當交換機 DPID 為 6 時，也會發送 Flow Statistics Request，用來取得
S6 的流表統計資料。

### 10.2 Port Statistics 速率計算

`port_stats_reply_handler()` 使用交換機回報的累積
`tx_bytes`、`rx_bytes`，與前一次統計值比較：

\[ Rate = `\frac{\Delta TX + \Delta RX}{1024 \times \Delta t}`{=tex} \]

其中： - (`\Delta `{=tex}TX)：兩次統計之間傳送位元組數的差值。 -
(`\Delta `{=tex}RX)：兩次統計之間接收位元組數的差值。 -
(`\Delta `{=tex}t)：兩次統計的時間間隔（秒）。

計算結果單位為 KB/s，儲存在 `self.port_stats[(dpid, port_no)]['rate']`。

第一次收到某個 Port 的統計資料時，程式只建立初始計數值，因此該次速率為
0；之後收到統計資料才可計算差值。

### 10.3 Flow Statistics 與 TCP 連線數

`flow_stats_reply_handler()` 只處理 S6 的 Flow Statistics，並篩選： -
Ethernet Type 為 IPv4。 - IP Protocol 為 TCP（6）。 - IPv4 Destination
為 VIP。 - TCP Destination Port 為 80。

符合條件的流表會用來更新連線追蹤資料，並依照連線原先記錄的伺服器，統計各伺服器的連線數。

------------------------------------------------------------------------

## 11. REST API 與監控介面資料

程式使用 Ryu WSGI 建立 API：

``` text
GET /stats/links
```

對應函式：

``` python
StatsController.get_stats()
```

它會呼叫：

``` python
self.app.get_ui_data()
```

並以 JSON 回傳資料。

### 11.1 API 回傳欄位

  欄位                         內容
  ---------------------------- ------------------------------
  `links`                      各節點間鏈路的流量速率
  `server_connections`         各伺服器目前記錄的連線數
  `server_traffic`             各伺服器的流量速率
  `server_load`                伺服器連線數、流量及負載分數
  `active_mid_switches`        目前啟用的中間交換機
  `current_mid_traffic_kbps`   Client 入口總流量（KB/s）
  `weights`                    連線數與流量的負載權重
  `is_blocked`                 目前固定回傳 `false`

`links` 中的速率資料由 `port_stats`
取得；交換機間鏈路的顯示值，是程式將連線兩端的 Port 速率相加後除以 2。

### 11.2 API 使用方式

在控制器啟動且 WSGI 服務可連線時，可使用：

``` bash
curl http://127.0.0.1:8080/stats/links
```

若 WSGI 使用不同埠號，請將網址中的 `8080` 改成實際設定值。

------------------------------------------------------------------------

## 12. 程式函式索引

  ------------------------------------------------------------------------------
  函式                                主要用途
  ----------------------------------- ------------------------------------------
  `__init__()`                        初始化控制器、伺服器、統計資料、API
                                      與監控執行緒

  `setup_topology_graph()`            建立交換機之間的 NetworkX 拓樸圖

  `get_least_loaded_mid_port()`       從目前啟用的中間交換機選擇負載較低的路徑

  `check_and_scale_mid_switches()`    依入口總流量執行中間交換機擴縮

  `switch_features_handler()`         交換機連線後安裝 Table-Miss Flow Entry

  `add_flow()`                        封裝並送出 OpenFlow FlowMod

  `flow_removed_handler()`            流表移除時釋放中間交換機的連線計數

  `_monitor()`                        定期向交換機要求 Port / Flow Statistics

  `port_stats_reply_handler()`        更新 Port 速率、入口總流量與伺服器流量

  `update_load_scores()`              更新伺服器綜合負載分數

  `flow_stats_reply_handler()`        根據 S6 流表統計更新伺服器連線數

  `get_least_loaded_server()`         選出負載分數最低的伺服器

  `install_tcp_connection_flows()`    安裝 TCP 連線的 DNAT 與 SNAT 流表

  `_packet_in_handler()`              主要封包解析、路徑選擇、NAT 與轉發邏輯

  `handle_arp_reply()`                回覆 VIP 的 ARP Request

  `get_ui_data()`                     整理監控介面需要的 JSON 資料

  `StatsController.get_stats()`       提供 REST API
  ------------------------------------------------------------------------------

------------------------------------------------------------------------

## 13. 安裝與執行

### 13.1 環境需求

本程式依賴以下元件：

-   Linux 執行環境。
-   Python。
-   Ryu SDN Framework。
-   Open vSwitch，並支援 OpenFlow 1.3。
-   Mininet（若使用 Mininet 建立實驗拓樸）。
-   Python 套件：`networkx`、`webob`，以及 Ryu 本身所需的相依套件。

建議使用已經能正常啟動 Ryu 的 Python 虛擬環境。Ryu 與 Python
版本的相容性可能因安裝方式而異，請以實際環境測試結果為準。

### 13.2 啟動控制器

假設程式檔名為 `sdn_load_balancer_nat.py`，可使用：

``` bash
ryu-manager --ofp-tcp-listen-port 6633 sdn_load_balancer_nat.py
```

若程式檔名不同，請替換成實際檔名。

### 13.3 Mininet / OVS 連線設定

請確認： 1. Mininet 中的交換機 DPID 為 1 至 6，且與程式設定一致。 2.
各交換機連接埠配置與第 2 節的表格一致。 3. OVS 使用 OpenFlow 1.3。 4.
交換機連線的 Controller IP 與 Port 指向 Ryu。 5. Client、VIP 與 Server
的 IP 設定與第 2.2 節一致。

可使用以下指令確認交換機 OpenFlow 設定（以 S1 為例）：

``` bash
sudo ovs-vsctl set bridge s1 protocols=OpenFlow13
sudo ovs-vsctl set-controller s1 tcp:<CONTROLLER_IP>:6633
```

其他交換機也需要設定相同的 OpenFlow 版本與正確 Controller 位址。

> 本 README 未包含 Mininet 拓樸建立程式，因此無法僅憑控制器程式確認主機
> IP、預設路由、連線頻寬及所有實體連接埠設定。請以實際拓樸程式為準。

------------------------------------------------------------------------

## 14. 測試與觀察建議

建議依序進行以下測試：

### 測試一：基本連線

-   確認六台交換機都成功連上 Ryu。
-   確認 Client 能夠解析 VIP 的 ARP。
-   確認 S6 能夠將封包送到 H3、H4。

### 測試二：伺服器負載平衡

-   讓 Client 對 VIP 的 TCP Port 80 建立多條新連線。
-   觀察控制器輸出的 `[LB] NEW TCP` 訊息。
-   確認新連線是否分配到 H3 或 H4。
-   觀察 API 中的 `server_connections`、`server_traffic` 與
    `server_load`。

### 測試三：中間交換機擴縮

-   逐步提高 Client 入口流量。
-   觀察總流量是否超過 2000 KB/s 與 5000 KB/s。
-   確認啟用清單是否由 `[3]` 變成 `[3, 4]`，再變成 `[3, 4, 5]`。
-   降低流量，確認是否依 4000 KB/s 與 1200 KB/s 門檻縮容。

### 測試四：多路徑分配

-   在不同流量條件下建立新的 TCP 連線。
-   觀察 `[PATH SELECTION]` 訊息。
-   確認新連線是否只分配到目前啟用的中間交換機。
-   比較各路徑的流量與連線數。

------------------------------------------------------------------------

## 15. 閱讀與維護時的重要注意事項

以下內容是依目前程式碼可觀察到的實作行為整理，維護或擴充功能時應特別留意。

1.  **擴縮判斷使用入口 Port 流量。** 程式將 S1 Port 1 與 S2 Port 1
    的速率相加，不是直接計算整個網路的總流量。
2.  **路徑負載是簡化估算。** `rate + conns * 500`
    是程式設定的評分方式，不等同於實際頻寬使用率或交換機硬體負載。
3.  **伺服器負載分數有上限。** 連線數與流量分數都會被限制在
    1.0，因此超過設定基準後不會繼續增加。
4.  **目前只處理 TCP/80 的 VIP 服務。** 若要支援其他 TCP Port、UDP
    或其他服務，需要修改封包比對與連線追蹤邏輯。
5.  **連線追蹤與流表生命週期需要一起檢查。**
    `connections`、`flow_to_mid_map`
    與交換機流表是不同資料結構，不能假設它們會在同一時間自動清除。
6.  **目前的連線統計有實作限制。** `flow_stats_reply_handler()` 會依 S6
    流表的 Match 條件辨識連線，但只會對已存在於 `self.connections`
    的項目增加伺服器連線數。
7.  **流量速率是取樣估算值。** 監控週期約 2
    秒，第一次收到統計資料時無法計算速率；短時間流量變化可能不會立即反映。
8.  **`flow_removed_handler()` 的計數釋放需核對 Match 欄位。** 程式以
    `ipv4_src` 與 `tcp_src` 尋找 `flow_to_mid_map`，而該 Map 的 Key 是
    `(src_ip, src_port)`；若移除事件未帶有預期欄位，計數可能無法正常釋放。
9.  **`client_mac_table` 依賴封包學習。** 若尚未學習到 Client MAC，SNAT
    使用的目的 MAC 可能會落入程式中的預設值。
10. **`setup_topology_graph()` 目前主要用於記錄拓樸。**
    目前路徑選擇是透過固定 DPID / Port 對應與啟用清單完成，並未使用
    NetworkX 執行最短路徑搜尋。

------------------------------------------------------------------------

## 16. 修改功能時，建議先看哪些地方？

  ---------------------------------------------------------------------------------------------
  想修改的功能                        優先查看
  ----------------------------------- ---------------------------------------------------------
  增加伺服器                          `self.SERVERS`、`get_least_loaded_server()`、S6 的 ARP /
                                      NAT 處理

  調整伺服器負載權重                  `CONN_WEIGHT`、`TRAFFIC_WEIGHT`、`update_load_scores()`

  調整伺服器負載基準                  `MAX_EXPECTED_CONNECTIONS`、`MAX_EXPECTED_TRAFFIC_KBPS`

  修改中間交換機擴縮門檻              `SCALE_UP_THRESHOLDS`、`SCALE_DOWN_THRESHOLDS`

  增加中間交換機                      `setup_topology_graph()`、Port
                                      對應、`active_mid_switches`、路徑選擇與封包轉發邏輯

  修改路徑負載算法                    `get_least_loaded_mid_port()`

  修改 NAT 規則                       `install_tcp_connection_flows()` 與
                                      `_packet_in_handler()`

  修改流量監控週期                    `_monitor()` 中的 `hub.sleep(2)`

  修改監控介面資料                    `get_ui_data()` 與 `StatsController`
  ---------------------------------------------------------------------------------------------

------------------------------------------------------------------------

## 17. 總結

本程式以 Ryu 作為 SDN 控制器，透過 OpenFlow 1.3 管理六台交換機。Client
封包進入網路後，控制器會依據目前的中間交換機啟用狀態與路徑負載，選擇適合的轉發路徑；封包抵達出口交換機
S6 後，再依伺服器的連線數與流量計算負載分數，將新 TCP
連線分配到負載較低的伺服器，並透過 DNAT / SNAT 維持 Client 與 VIP
之間的通訊。

此外，控制器會定期收集交換機 Port Statistics 與 S6 的 Flow
Statistics，更新流量、連線數及負載資訊，並透過 REST API
提供監控介面使用。

閱讀程式時，建議先從 `__init__()` 了解設定與資料結構，再依序閱讀
`_monitor()`、`port_stats_reply_handler()`、`check_and_scale_mid_switches()`、`get_least_loaded_mid_port()`，最後閱讀
`_packet_in_handler()` 與
`install_tcp_connection_flows()`，即可掌握整個系統從監控、決策到封包轉發的主要流程。
