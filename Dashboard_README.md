# SDN Dashboard 即時監控介面

本文件說明本專案的 Dashboard
程式，協助後續開發者了解介面架構、資料來源、更新流程、拓樸視覺化、流量圖表，以及如何安裝、啟動與維護。

Dashboard 使用 **Python、PyQt6、PyQtGraph、NetworkX** 開發，透過 HTTP
REST API 向 Ryu Controller
取得網路統計資料，並以圖形化方式呈現交換機拓樸、鏈路流量、伺服器 TCP
連線數與負載狀態。

> 本文件依據目前提供的 Dashboard 程式碼撰寫。Dashboard
> 本身負責資料讀取與視覺化，不負責執行 SDN 路徑選擇、伺服器分配或
> NAT；這些控制決策由 Ryu Controller 負責。

------------------------------------------------------------------------

## 1. Dashboard 功能總覽

目前 Dashboard 提供兩個主要頁面：

1.  **拓樸即時監控**
    -   顯示 Client、Switch、Server 之間的網路拓樸。
    -   顯示各鏈路的即時流量速率。
    -   依鏈路流量改變線條顏色與粗細。
    -   以節點顏色標示中間交換機是否有流量經過。
    -   以動畫圓點示意資料流經鏈路。
    -   在 H3、H4 節點上顯示 TCP 連線數、流量與負載分數。
2.  **負載與連線圖表**
    -   顯示 H3、H4 的即時接收流量比較。
    -   顯示 H3、H4 的 TCP 連線數比較。
    -   以時間序列方式更新曲線，方便觀察兩台伺服器的負載變化。

Dashboard 每隔約 2 秒向 Ryu Controller 的 REST API 取得一次資料。

------------------------------------------------------------------------

## 2. 系統架構與資料流

Dashboard 與 Ryu Controller 的資料交換方式如下：

``` text
┌──────────────────────────────┐
│         Ryu Controller       │
│                              │
│  OpenFlow Statistics         │
│  Port / Flow Statistics      │
│                              │
│  GET /stats/links            │
└──────────────┬───────────────┘
               │ HTTP + JSON
               │ 約每 2 秒請求一次
               ▼
┌──────────────────────────────┐
│        DataFetchThread       │
│                              │
│  requests.get(STATS_URL)     │
│  取得 JSON 並發出 Signal     │
└──────────────┬───────────────┘
               │ data_received
               ▼
┌──────────────────────────────┐
│        MainApplication       │
│                              │
│  dispatch_data(data)         │
│       │                      │
│       ├──────────────┐       │
│       ▼              ▼       │
│  TopologyWidget  ChartWidget │
│  網路拓樸監控     負載圖表    │
└──────────────────────────────┘
```

### 2.1 資料取得網址

程式目前設定：

``` python
STATS_URL = 'http://localhost:8080/stats/links'
```

因此 Dashboard 預設會連線到本機的 Ryu REST API。

如果 Ryu Controller 與 Dashboard 不在同一台電腦，必須將 `localhost` 改成
Ryu Controller 所在主機的 IP 位址，並確認 API 服務可從 Dashboard
主機連線。

### 2.2 Controller 回傳資料格式

Dashboard 主要使用以下 JSON 欄位：

  欄位                   用途                         使用位置
  ---------------------- ---------------------------- --------------------
  `links`                鏈路來源、目的地與流量速率   拓樸頁面
  `server_connections`   各伺服器 TCP 連線數          拓樸節點、連線圖表
  `server_traffic`       各伺服器流量速率             拓樸節點、流量圖表
  `server_load`          各伺服器負載分數等資訊       H3、H4 節點

`links` 預期為陣列，每筆資料包含：

``` json
{
  "source": "s1",
  "target": "s3",
  "rate": 250.0
}
```

伺服器相關資料的格式例如：

``` json
{
  "server_connections": {
    "h3": 12,
    "h4": 8
  },
  "server_traffic": {
    "h3": 1200.0,
    "h4": 850.0
  },
  "server_load": {
    "h3": {
      "load_score": 0.42
    },
    "h4": {
      "load_score": 0.18
    }
  }
}
```

以上是欄位結構示意，數值僅為範例。實際資料由 Ryu Controller 提供。

------------------------------------------------------------------------

## 3. 程式架構

Dashboard 主要由以下類別組成：

  類別                功能
  ------------------- ----------------------------------------
  `DataFetchThread`   在背景執行緒向 Ryu API 取得資料
  `NodeItem`          建立與更新拓樸中的節點
  `LinkItem`          建立與更新節點之間的鏈路
  `PacketItem`        建立鏈路上的資料流動畫圓點
  `TopologyWidget`    建立網路拓樸頁面並整合節點、鏈路與動畫
  `ChartWidget`       建立伺服器流量及 TCP 連線數圖表
  `MainApplication`   建立主視窗、頁面切換與資料分派

程式主要執行順序：

1.  建立 `QApplication`。
2.  建立 `MainApplication` 主視窗。
3.  初始化工具列與 `QStackedWidget`。
4.  建立 `TopologyWidget` 與 `ChartWidget`。
5.  啟動 `DataFetchThread`。
6.  背景執行緒定期取得 Controller 的 JSON 資料。
7.  透過 `data_received` Signal 將資料傳回主視窗。
8.  `dispatch_data()` 將資料分別交給拓樸頁面與圖表頁面更新。

------------------------------------------------------------------------

## 4. 背景資料取得：DataFetchThread

### 4.1 功能

`DataFetchThread` 繼承 PyQt6 的 `QThread`，負責在背景執行 HTTP
請求，避免網路請求占用 GUI 主執行緒，造成畫面操作卡頓。

主要程式：

``` python
class DataFetchThread(QThread):
    data_received = pyqtSignal(object)

    def run(self):
        while True:
            try:
                response = requests.get(STATS_URL, timeout=1.5)
                if response.status_code == 200:
                    data = response.json()
                    self.data_received.emit(data)
            except Exception:
                pass

            self.msleep(2000)
```

### 4.2 執行流程

1.  向 `STATS_URL` 發送 GET 請求。
2.  設定 HTTP Timeout 為 1.5 秒。
3.  若 HTTP 狀態碼為 200，將回應解析成 JSON。
4.  透過 `data_received` Signal 傳送資料。
5.  等待 2000 毫秒後進行下一次請求。

### 4.3 為什麼使用 Signal？

PyQt 的 GUI 元件應由 GUI 主執行緒更新。背景執行緒取得資料後，使用 Signal
將資料傳回主視窗，再由主視窗更新介面，可以避免直接從背景執行緒操作 GUI
元件。

### 4.4 目前實作注意事項

-   HTTP 請求失敗或 JSON 解析發生例外時，目前使用
    `except Exception: pass`，不會顯示錯誤訊息。
-   目前 `while True`
    沒有提供停止條件，若未來需要正常關閉或重新啟動執行緒，建議加入停止旗標與結束處理。
-   請求間隔約 2 秒，但若 HTTP
    請求本身耗時，實際更新週期會是請求時間加上等待時間。

------------------------------------------------------------------------

## 5. 拓樸即時監控：TopologyWidget

### 5.1 功能

`TopologyWidget` 使用 `QGraphicsScene` 與 `QGraphicsView`
繪製網路拓樸，並透過 NetworkX 建立節點與連線關係。

目前拓樸包含：

-   Client：H1、H2。
-   入口交換機：S1、S2。
-   中間交換機：S3、S4、S5。
-   NAT / Load Balancer 出口交換機：S6。
-   Server：H3、H4。

### 5.2 拓樸連線

程式在 `init_topology_layout()` 中建立以下連線：

``` text
H1 ─ S1 ─┬─ S3 ─┐
         ├─ S4 ─┼─ S6 ─ H3
         └─ S5 ─┘     └─ H4

H2 ─ S2 ─┬─ S3
         ├─ S4
         └─ S5
```

此圖為邏輯拓樸示意。S1、S2 都能連接到 S3、S4、S5，再由中間交換機連接到
S6。

### 5.3 節點位置

目前使用固定座標配置節點，將網路分成五個階段：

  階段                  節點
  --------------------- ------------
  Client                H1、H2
  Ingress               S1、S2
  Mid Switch            S3、S4、S5
  NAT / Load Balancer   S6
  Server                H3、H4

若要調整 Dashboard 的拓樸外觀，可修改 `init_topology_layout()` 中的
`pos` 字典。

### 5.4 節點顏色

`NodeItem` 依照節點角色設定初始顏色：

  節點             顏色             意義
  ---------------- ---------------- ------------------------
  S1、S2           藍色 `#3498db`   入口交換機
  S3、S4、S5       灰色 `#95a5a6`   中間交換機，預設未亮起
  S6               橘色 `#e67e22`   NAT / Load Balancer
  H1、H2、H3、H4   綠色 `#2ecc71`   Host / Server

中間交換機會在更新資料時依照鏈路流量改變顏色，詳見第 6 節。

### 5.5 Server 節點資訊

H3 與 H4 節點會額外顯示：

-   TCP 連線數。
-   目前流量。
-   Load Score（若 API 有提供）。

例如：

``` text
h3
TCP: 12 C
1.17 MB/s
Load: 0.420
```

程式收到的 `server_traffic` 單位為 KB/s，顯示時會除以 1024，轉換成
MB/s。

------------------------------------------------------------------------

## 6. 鏈路流量與路徑視覺化：LinkItem

### 6.1 鏈路流量顯示

`LinkItem` 負責繪製兩個節點之間的線條，並在鏈路中間顯示流量速率。

``` python
def update_rate(self, rate):
    self.text.setPlainText(f"{int(rate)} KB/s")
```

流量數值會顯示在鏈路上，方便觀察不同鏈路的流量變化。

### 6.2 鏈路顏色與粗細

目前依照流量速率使用以下規則：

  流量條件              顏色     線條粗細 視覺意義
  --------------------- ------ ---------- ----------
  `rate == 0`           灰色            2 無流量
  `0 < rate < 300`      綠色            3 低流量
  `300 <= rate < 900`   黃色            4 中等流量
  `rate >= 900`         紅色            5 較高流量

單位為 KB/s。

這些數值是 Dashboard 的視覺化分級門檻，不是 Ryu Controller
的交換機擴縮門檻，也不代表實際網路壅塞判定。

### 6.3 中間交換機亮起條件

在 `TopologyWidget.update_ui()` 中：

``` python
if rate > 5.0:
    for sw in ['s3', 's4', 's5']:
        if sw in link_key:
            active_paths.add(sw)
```

當某筆鏈路流量大於 5 KB/s，且該鏈路連接到 S3、S4 或 S5
時，對應的中間交換機會被標記為有流量經過。

接著：

-   有符合條件的流量：中間交換機顯示藍色。
-   沒有符合條件的流量：中間交換機顯示灰色。

> **重要：** 這是 Dashboard 根據 API
> 鏈路速率進行的視覺化判斷。它不是直接讀取交換機的啟用狀態，也不是控制器的
> `active_mid_switches` 欄位。因此，節點變藍代表符合 Dashboard
> 的流量顯示條件，不一定等同於控制器目前將該交換機列為啟用狀態。

------------------------------------------------------------------------

## 7. 封包動畫：PacketItem

### 7.1 功能

`PacketItem` 使用紅色小圓點，示意資料在拓樸鏈路上移動。

動畫流程：

1.  `trigger_packet_animation(src, dst)` 收到鏈路兩端節點名稱。
2.  找到目前沒有顯示的動畫圓點。
3.  呼叫 `send()` 設定起點與終點。
4.  `QTimer` 定期呼叫 `map_tick()`。
5.  `map_tick()` 更新每個動畫圓點的位置。
6.  圓點移動到終點後隱藏。

### 7.2 動畫參數

  參數                                 設定 意義
  --------------- ------------------------- ------------------------
  動畫圓點數量                           20 最多建立 20 個動畫物件
  `total_steps`                          20 每次動畫移動的步數
  `anim_timer`                        30 ms 每隔約 30 ms 更新動畫
  觸發條件                     `rate > 5.0` 鏈路流量超過 5 KB/s
  隨機觸發          `random.random() > 0.4` 約 60% 機率觸發動畫

### 7.3 動畫資料的意義

紅色圓點是依照鏈路流量條件產生的視覺化動畫，**不是實際從 Mininet 或
OpenFlow 擷取的單一封包，也不代表真實封包的精確傳輸時間與路徑**。

若要呈現真實封包資訊，需要額外蒐集封包事件或封包追蹤資料，不能只依靠目前的鏈路速率統計。

------------------------------------------------------------------------

## 8. 負載與連線圖表：ChartWidget

### 8.1 功能

`ChartWidget` 使用 PyQtGraph 繪製兩張即時折線圖：

1.  伺服器實時接收流量（H3 vs H4）。
2.  伺服器實時 TCP 連線數（H3 vs H4）。

兩張圖共用時間軸，方便對照流量與連線數的變化。

### 8.2 流量圖表

標題：

``` text
伺服器實時接收流量 (h3 vs h4)
```

  曲線   顏色   資料來源
  ------ ------ ---------------------
  H3     綠色   `server_traffic.h3`
  H4     黃色   `server_traffic.h4`

縱軸單位為 KB/s，橫軸為相對時間（秒）。

### 8.3 TCP 連線數圖表

標題：

``` text
伺服器實時 TCP 連線數 (負載平衡驗證)
```

  曲線   顏色   資料來源
  ------ ------ -------------------------
  H3     綠色   `server_connections.h3`
  H4     黃色   `server_connections.h4`

縱軸單位為筆數，程式目前將 Y 軸範圍設定為 0 至 100。

### 8.4 歷史資料保存方式

程式目前設定：

``` python
self.sample_interval = 2
self.time_history = [i * self.sample_interval for i in range(-29, 1)]

self.h3_traffic_history = [0.0] * 30
self.h4_traffic_history = [0.0] * 30
self.h3_conn_history = [0] * 30
self.h4_conn_history = [0] * 30
```

每次更新時： 1. 移除歷史資料最舊的一筆。 2. 將新資料加入陣列尾端。 3.
更新時間軸。 4. 將資料傳給 PyQtGraph 曲線。

目前每張圖保留 30 個資料點，每個資料點的時間間隔設定為 2 秒，因此顯示約
60 秒的歷史資料。

注意：這裡的時間是 Dashboard
自行累加的相對時間，不是從系統時鐘取得的絕對時間。

------------------------------------------------------------------------

## 9. 主視窗與頁面切換：MainApplication

### 9.1 主視窗

`MainApplication` 繼承 `QMainWindow`，負責： - 設定視窗標題與大小。 -
建立工具列。 - 建立頁面容器 `QStackedWidget`。 - 建立拓樸與圖表頁面。 -
啟動資料取得執行緒。 - 接收資料並分派給各個頁面。

視窗標題：

``` text
SDN Real-Time Network Suite
```

預設視窗大小為 1000 × 800。

### 9.2 工具列

目前有兩個切換項目：

  項目             顯示頁面
  ---------------- ------------------
  拓樸即時監控     `TopologyWidget`
  負載與連線圖表   `ChartWidget`

切換時由 `switch_view(index, clicked_action)` 更新 `QStackedWidget`
顯示的頁面，並同步更新工具列按鈕的選取狀態。

### 9.3 資料分派

`dispatch_data(data)` 是 Dashboard 的資料分派入口。

它會從 JSON 中取出：

``` python
links_list = data.get('links', [])
server_conns = data.get('server_connections', {})
server_traffic = data.get('server_traffic', {})
server_load = data.get('server_load', {})
```

接著： - 將鏈路與伺服器資料交給 `TopologyWidget.update_ui()`。 -
將完整資料交給 `ChartWidget.update_ui()`。

------------------------------------------------------------------------

## 10. 安裝環境與套件需求

### 10.1 必要套件

Dashboard 使用以下 Python 套件：

  套件          用途
  ------------- --------------------------------
  `PyQt6`       建立桌面 GUI
  `pyqtgraph`   繪製即時折線圖
  `networkx`    建立拓樸圖的節點與邊
  `requests`    向 Ryu REST API 發送 HTTP 請求

另外使用 Python 標準函式庫： - `random` - `sys`

### 10.2 安裝套件

建議在 Python 虛擬環境中安裝：

``` bash
python -m venv dashboard-env
```

Linux / macOS：

``` bash
source dashboard-env/bin/activate
```

Windows：

``` powershell
dashboard-env\Scripts\activate
```

安裝依賴：

``` bash
pip install PyQt6 pyqtgraph networkx requests
```

### 10.3 requirements.txt 範例

可在 Dashboard 專案目錄建立 `requirements.txt`：

``` text
PyQt6
pyqtgraph
networkx
requests
```

之後可透過以下指令安裝：

``` bash
pip install -r requirements.txt
```

------------------------------------------------------------------------

## 11. 啟動方式

假設 Dashboard 程式檔名為：

``` text
dashboard.py
```

### 11.1 先啟動 Ryu Controller

確認 Ryu Controller 已經啟動，並且 REST API 可正常回應：

``` bash
curl http://localhost:8080/stats/links
```

若 API 正常，應回傳 JSON 格式的網路統計資料。

### 11.2 再啟動 Dashboard

在啟用 Dashboard 虛擬環境後執行：

``` bash
python dashboard.py
```

程式啟動後會開啟桌面視窗，並開始背景資料抓取。

### 11.3 啟動順序

建議依照以下順序啟動：

1.  啟動 Mininet 與 Open vSwitch 拓樸。
2.  啟動 Ryu Controller。
3.  確認交換機已連線到 Controller。
4.  確認 `/stats/links` API 可正常取得 JSON。
5.  啟動 Dashboard。
6.  開始產生測試流量，觀察拓樸與圖表變化。

------------------------------------------------------------------------

## 12. 常見問題排查

### 12.1 Dashboard 沒有資料或畫面沒有更新

可能原因： - Ryu Controller 尚未啟動。 - REST API 位址或 Port
設定錯誤。 - Ryu 的 WSGI API 沒有正常提供服務。 - Dashboard 與
Controller 不在同一台主機，但仍使用 `localhost`。 - API 回傳格式與
Dashboard 預期欄位不一致。

建議先執行：

``` bash
curl http://localhost:8080/stats/links
```

確認是否能取得 JSON。

### 12.2 拓樸有顯示，但鏈路流量一直是 0

可能原因： - Controller 尚未收到新的 Port Statistics。 -
測試環境沒有產生流量。 - Controller 的 Port 對應與實際拓樸不一致。 - API
中 `links` 欄位沒有正確提供 `source`、`target`、`rate`。

### 12.3 H3、H4 沒有顯示連線數或負載

請確認 API 是否包含：

``` json
{
  "server_connections": {},
  "server_traffic": {},
  "server_load": {}
}
```

並確認伺服器名稱使用 `h3`、`h4`，大小寫必須一致。

### 12.4 Dashboard 啟動時出現 ModuleNotFoundError

代表目前 Python 環境缺少相應套件。請確認已啟用正確的虛擬環境，並執行：

``` bash
pip install PyQt6 pyqtgraph networkx requests
```

### 12.5 視窗關閉後執行緒處理需要改善

目前 `DataFetchThread.run()` 使用無限迴圈，沒有停止條件。若未來要將
Dashboard 作為長期使用的監控工具，建議實作停止旗標、逾時處理與
`closeEvent()`，讓視窗關閉時能正常停止背景執行緒。

------------------------------------------------------------------------

## 13. 函式索引

  -------------------------------------------------------------------------------------
  類別                    函式                           用途
  ----------------------- ------------------------------ ------------------------------
  `DataFetchThread`       `run()`                        定期向 Controller 取得 JSON

  `NodeItem`              `__init__()`                   建立拓樸節點

  `NodeItem`              `update_text_position()`       更新節點文字位置

  `NodeItem`              `set_node_color()`             更新節點顏色

  `NodeItem`              `update_server_info()`         更新 H3、H4 的伺服器資訊

  `LinkItem`              `__init__()`                   建立鏈路與流量文字

  `LinkItem`              `update_position()`            更新鏈路文字位置

  `LinkItem`              `update_rate()`                更新流量數值、顏色與線條粗細

  `PacketItem`            `send()`                       設定動畫起點與終點

  `PacketItem`            `update_animation()`           更新動畫圓點位置

  `TopologyWidget`        `init_topology_layout()`       建立拓樸節點、連線與位置

  `TopologyWidget`        `map_tick()`                   定期更新動畫

  `TopologyWidget`        `update_ui()`                  更新鏈路、節點與伺服器資訊

  `TopologyWidget`        `trigger_packet_animation()`   觸發鏈路動畫

  `ChartWidget`           `__init__()`                   建立流量與連線數圖表

  `ChartWidget`           `update_ui()`                  更新歷史資料與曲線

  `MainApplication`       `__init__()`                   建立主視窗與啟動資料執行緒

  `MainApplication`       `create_menu_toolbar()`        建立頁面切換工具列

  `MainApplication`       `switch_view()`                切換目前顯示頁面

  `MainApplication`       `dispatch_data()`              將 API 資料分派至各頁面
  -------------------------------------------------------------------------------------

------------------------------------------------------------------------

## 14. 後續擴充建議

若要繼續擴充 Dashboard，可以從以下方向著手：

1.  **增加連線狀態提示**：當 API
    無法連線時，在介面顯示警告，而不是讓畫面看起來像是沒有流量。
2.  **顯示交換機實際啟用狀態**：使用 Controller 回傳的
    `active_mid_switches`，區分「交換機已啟用」與「目前觀察到流量」。
3.  **加入流量單位切換**：提供 KB/s、MB/s 等顯示方式。
4.  **增加歷史資料保存**：將資料寫入 CSV 或資料庫，供實驗後分析。
5.  **改善執行緒結束流程**：加入停止旗標與例外紀錄，提升長時間執行的穩定性。
6.  **加入更多監控指標**：例如封包數、丟包數、延遲、各鏈路使用率等；前提是
    Controller API 也必須提供對應資料。
7.  **讓拓樸由 API 動態取得**：減少 Dashboard 與 Mininet
    拓樸設定不一致的問題。

------------------------------------------------------------------------

## 15. 總結

本 Dashboard 是 SDN 網路管理系統的圖形化監控介面。它透過背景執行緒定期向
Ryu Controller 的 REST API
取得網路統計資料，再將資料分派至拓樸監控與負載圖表兩個頁面。

拓樸頁面主要呈現鏈路流量、交換機路徑活動與伺服器資訊；圖表頁面則用來比較
H3、H4 的流量與 TCP
連線數變化。透過這些資訊，使用者可以觀察流量分配與伺服器負載平衡的實驗結果。

閱讀程式時，建議先從 `MainApplication` 了解主視窗與資料分派，再閱讀
`DataFetchThread` 理解資料來源，接著依序閱讀
`TopologyWidget`、`NodeItem`、`LinkItem`、`PacketItem`，最後閱讀
`ChartWidget`，即可掌握 Dashboard 從取得資料到呈現監控畫面的完整流程。
