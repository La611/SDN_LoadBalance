# 程式執行

以下依序在三個終端機視窗執行。
**啟動順序：** Ryu Controller → Mininet Topology → Dashboard

## 1. 啟動 Ryu Controller

``` bash
cd ~/SDN-Load-Balancing
source ryu-env/bin/activate
ryu-manager --observe-links Your_Controller_Name.py
```
<img width="828" height="400" alt="image" src="https://github.com/user-attachments/assets/1d382678-37ed-4181-a71d-fb26e13e27bd" />

## 2. 啟動 Mininet Topology

``` bash
cd ~/SDN-Load-Balancing
sudo python3 Topo_Name.py
```
<img width="544" height="242" alt="image" src="https://github.com/user-attachments/assets/a0ca0be8-5883-4c1a-ab15-499b8b804ca8" />

## 3. 啟動 Dashboard

``` bash
cd ~/SDN-Load-Balancing
source dashboard-env/bin/activate
python UI_Name.py
```
<img width="701" height="302" alt="image" src="https://github.com/user-attachments/assets/8b4feb27-aa7d-41f3-a8ff-bd49f1f82c01" />

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

