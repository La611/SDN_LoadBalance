# SDN 負載平衡系統啟動指令

以下依序在三個終端機視窗執行。

## 1. 啟動 Ryu Controller

``` bash
cd ~/SDN-Load-Balancing
source ryu-env/bin/activate
ryu-manager --ofp-tcp-listen-port 6633 Controller.py
```

## 2. 啟動 Mininet Topology

``` bash
cd ~/SDN-Load-Balancing
sudo python3 Topo.py
```

## 3. 啟動 Dashboard

``` bash
cd ~/SDN-Load-Balancing
source dashboard-env/bin/activate
python UI.py
```

## 4. 確認 REST API

可在另一個終端機執行：

``` bash
curl http://localhost:8080/stats/links
```

## 5. Mininet 基本測試

在 Mininet CLI 中執行：

``` text
pingall
```

查看 OpenFlow 流表：

``` bash
sudo ovs-ofctl -O OpenFlow13 dump-flows s1
```

## 6. 關閉系統

1.  在 Mininet CLI 輸入 `exit`。
2.  在 Ryu Controller 終端機按 `Ctrl+C`。
3.  清除 Mininet 環境：

``` bash
sudo mn -c
```

------------------------------------------------------------------------

**啟動順序：** Ryu Controller → Mininet Topology → Dashboard

**注意：** 上述指令假設專案資料夾名稱為
`SDN-Load-Balancing`，且程式檔名分別為
`Controller.py`、`Topo.py`、`UI.py`。若你的實際資料夾或檔名不同，請替換成實際名稱。
