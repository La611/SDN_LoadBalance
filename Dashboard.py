import random
import sys
import networkx as nx
import pyqtgraph as pg
import requests
from PyQt6.QtCore import QPointF, QThread, QTimer, Qt, pyqtSignal

# 關鍵修正：將 QAction 放進 QtGui 引用，並從 QtWidgets 中移除
from PyQt6.QtGui import QAction, QBrush, QColor, QFont, QPen
from PyQt6.QtWidgets import (
    QApplication,
    QGraphicsScene,
    QGraphicsView,
    QMainWindow,
    QStackedWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

STATS_URL = 'http://localhost:8080/stats/links'


# --- 1. 背景資料抓取執行緒 ---
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


# --- 2. 拓樸繪圖元件 (Node, Link, Packet) ---
class NodeItem:
    def __init__(self, scene, name, x, y):
        self.name = name
        self.pos = QPointF(x, y)
        self.radius = 24

        # 根據 Switch / Host 角色進行配色
        if name in ['s1', 's2']:
            color = '#3498db'  # Ingress Switch (藍色)
        elif name in ['s3', 's4', 's5']:
            color = '#95a5a6'  # Mid Switch (預設灰色)
        elif name == 's6':
            color = '#e67e22'  # NAT / LB Switch (橘黃色)
        else:
            color = '#2ecc71'  # Host h1, h2, h3, h4 (綠色)

        self.ellipse = scene.addEllipse(
            x - self.radius, y - self.radius,
            self.radius * 2, self.radius * 2,
            QPen(QColor('#2c3e50'), 2),
            QBrush(QColor(color))
        )

        self.text = scene.addText(name, QFont('Arial', 9, QFont.Weight.Bold))
        self.text.setDefaultTextColor(QColor('#ffffff'))
        self.update_text_position()

    def update_text_position(self):
        rect = self.text.boundingRect()
        self.text.setPos(self.pos.x() - rect.width() / 2, self.pos.y() - rect.height() / 2)

    def set_node_color(self, hex_color):
        self.ellipse.setBrush(QBrush(QColor(hex_color)))

    def update_server_info(self, conn_count=0, traffic=0.0, load_score=None):
        if self.name not in ['h3', 'h4']:
            return

        # 拓樸節點直接顯示：
        # TCP 連線數、目前流量、Load Score
        if load_score is None:
            self.text.setPlainText(
                f"{self.name}\n"
                f"TCP: {conn_count} C\n"
                f"{traffic / 1024.0:.2f} MB/s"
            )
        else:
            self.text.setPlainText(
                f"{self.name}\n"
                f"TCP: {conn_count} C\n"
                f"{traffic / 1024.0:.2f} MB/s\n"
                f"Load: {load_score:.3f}"
            )

        self.update_text_position()


class LinkItem:
    def __init__(self, scene, node_a, node_b):
        self.scene = scene
        self.node_a = node_a
        self.node_b = node_b

        self.line = scene.addLine(
            node_a.pos.x(), node_a.pos.y(),
            node_b.pos.x(), node_b.pos.y(),
            QPen(QColor('#7f8c8d'), 2)
        )

        self.text = scene.addText('0 KB/s', QFont('Arial', 8))
        self.text.setDefaultTextColor(QColor('#2c3e50'))
        self.update_position()

    def update_position(self):
        mid_x = (self.node_a.pos.x() + self.node_b.pos.x()) / 2
        mid_y = (self.node_a.pos.y() + self.node_b.pos.y()) / 2
        self.text.setPos(mid_x - 20, mid_y - 10)

    def update_rate(self, rate):
        self.text.setPlainText(f"{int(rate)} KB/s")
        if rate == 0.0:
            color, width = '#7f8c8d', 2
        elif rate < 300:
            color, width = '#2ecc71', 3
        elif rate < 900:
            color, width = '#f1c40f', 4
        else:
            color, width = '#e74c3c', 5
        self.line.setPen(QPen(QColor(color), width))


class PacketItem:
    def __init__(self, scene):
        self.radius = 5
        self.dot = scene.addEllipse(
            0, 0, self.radius * 2, self.radius * 2,
            QPen(Qt.PenStyle.NoPen), QBrush(QColor('#e74c3c'))
        )
        self.dot.hide()
        self.current_step = 0
        self.total_steps = 20

    def send(self, start_node, end_node):
        self.start_p = start_node.pos
        self.end_p = end_node.pos
        self.current_step = 0
        self.dot.show()

    def update_animation(self):
        if not self.dot.isVisible():
            return
        self.current_step += 1
        if self.current_step <= self.total_steps:
            t = self.current_step / self.total_steps
            curr_x = self.start_p.x() + (self.end_p.x() - self.start_p.x()) * t
            curr_y = self.start_p.y() + (self.end_p.y() - self.start_p.y()) * t
            self.dot.setPos(curr_x - self.radius, curr_y - self.radius)
        else:
            self.dot.hide()


# --- 3. 拓樸視窗 Widget ---
class TopologyWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)

        self.scene = QGraphicsScene(self)
        self.scene.setSceneRect(0, 0, 950, 650)
        self.view = QGraphicsView(self.scene, self)
        self.view.setRenderHint(self.view.renderHints().Antialiasing)
        layout.addWidget(self.view)

        self.nodes = {}
        self.links_dict = {}
        self.packets = [PacketItem(self.scene) for _ in range(20)]

        self.init_topology_layout()

        self.anim_timer = QTimer(self)
        self.anim_timer.timeout.connect(self.map_tick)
        self.anim_timer.start(30)

    def init_topology_layout(self):
        G = nx.Graph()
        switches = ['s1', 's2', 's3', 's4', 's5', 's6']
        hosts = ['h1', 'h2', 'h3', 'h4']

        for node in switches + hosts:
            G.add_node(node)

        # 根據 AdvancedTopo.py 設定連線
        G.add_edge('h1', 's1')
        G.add_edge('h2', 's2')
        for ingress in ['s1', 's2']:
            for mid in ['s3', 's4', 's5']:
                G.add_edge(ingress, mid)
        for mid in ['s3', 's4', 's5']:
            G.add_edge(mid, 's6')
        G.add_edge('s6', 'h3')
        G.add_edge('s6', 'h4')

        # 精確佈局：橫向分為 5 個階段 (Clients -> Ingress -> Mid -> NAT/LB -> Servers)
        pos = {
            'h1': (80, 200),
            'h2': (80, 450),
            's1': (260, 200),
            's2': (260, 450),
            's3': (475, 150),
            's4': (475, 325),
            's5': (475, 500),
            's6': (690, 325),
            'h3': (870, 200),
            'h4': (870, 450)
        }

        for node_name, coords in pos.items():
            self.nodes[node_name] = NodeItem(self.scene, node_name, coords[0], coords[1])

        for src, dst in G.edges():
            link_item = LinkItem(self.scene, self.nodes[src], self.nodes[dst])
            link_key = tuple(sorted([src, dst]))
            self.links_dict[link_key] = link_item

    def map_tick(self):
        for p in self.packets:
            p.update_animation()

    def update_ui(
        self,
        links_list,
        server_conns=None,
        server_traffic=None,
        server_load=None
    ):
        active_paths = set()

        if not isinstance(server_conns, dict):
            server_conns = {}

        if not isinstance(server_traffic, dict):
            server_traffic = {}

        if not isinstance(server_load, dict):
            server_load = {}

        for item in links_list:
            src = item.get('source')
            dst = item.get('target')
            rate = item.get('rate', 0.0)
            link_key = tuple(sorted([src, dst]))

            if link_key in self.links_dict:
                self.links_dict[link_key].update_rate(rate)

                if rate > 5.0:
                    for sw in ['s3', 's4', 's5']:
                        if sw in link_key:
                            active_paths.add(sw)

                if rate > 5.0 and random.random() > 0.4:
                    self.trigger_packet_animation(src, dst)

        # 讓動態流經的中繼 Switch (s3, s4, s5) 變色亮起
        for sw_name in ['s3', 's4', 's5']:
            if sw_name in self.nodes:
                color = '#3498db' if sw_name in active_paths else '#95a5a6'
                self.nodes[sw_name].set_node_color(color)

        # 更新 h3 / h4 Server 狀態
        for srv_name in ['h3', 'h4']:
            if srv_name in self.nodes:
                conns = server_conns.get(srv_name, 0)
                traffic = server_traffic.get(srv_name, 0.0)

                # 支援：
                # server_load = {
                #     'h3': {'load_score': 0.42, ...},
                #     'h4': {'load_score': 0.18, ...}
                # }
                load_info = server_load.get(srv_name, {})
                if isinstance(load_info, dict):
                    score = load_info.get('load_score', None)
                else:
                    score = None

                self.nodes[srv_name].update_server_info(
                    conn_count=conns,
                    traffic=traffic,
                    load_score=score
                )

    def trigger_packet_animation(self, src, dst):
        if src in self.nodes and dst in self.nodes:
            for p in self.packets:
                if not p.dot.isVisible():
                    p.send(self.nodes[src], self.nodes[dst])
                    break


# --- 4. 流量圖表 Widget ---
class ChartWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)

        self.win = pg.GraphicsLayoutWidget(show=True)
        layout.addWidget(self.win)

        # 1. 流量與丟包圖表
        self.plot_traffic = self.win.addPlot(title="伺服器實時接收流量 (h3 vs h4)")
        self.plot_traffic.setLabel('left', '接收流量', units='KB/s', color='#2ecc71')
        self.plot_traffic.setLabel('bottom', '相對時間 (秒)')
        self.plot_traffic.showGrid(x=True, y=True)
        self.plot_traffic.addLegend(offset=(10, 10))

        self.curve_h3_traffic = self.plot_traffic.plot(
            pen=pg.mkPen(color='#2ecc71', width=3), name='h3 (192.168.0.10)'
        )
        self.curve_h4_traffic = self.plot_traffic.plot(
            pen=pg.mkPen(color='#f1c40f', width=3), name='h4 (192.168.0.20)'
        )

        self.win.nextRow()

        # 2. 連線數圖表
        self.plot_conn = self.win.addPlot(title="伺服器實時 TCP 連線數 (負載平衡驗證)")
        self.plot_conn.setLabel('left', '連線數量', units='筆', color='#3498db')
        self.plot_conn.setLabel('bottom', '相對時間 (秒)')
        self.plot_conn.showGrid(x=True, y=True)
        self.plot_conn.addLegend(offset=(10, 10))
        self.plot_conn.setXLink(self.plot_traffic)
        self.plot_conn.setYRange(0, 100)
        self.curve_h3_conn = self.plot_conn.plot(
            pen=pg.mkPen(color='#2ecc71', width=3), name='h3 TCP 連線數'
        )
        self.curve_h4_conn = self.plot_conn.plot(
            pen=pg.mkPen(color='#f1c40f', width=3), name='h4 TCP 連線數'
        )

        self.sample_interval = 2
        self.current_time = 0
        self.time_history = [i * self.sample_interval for i in range(-29, 1)]

        self.h3_traffic_history = [0.0] * 30
        self.h4_traffic_history = [0.0] * 30
        self.h3_conn_history = [0] * 30
        self.h4_conn_history = [0] * 30

    def update_ui(self, raw_data):
        h3_traffic = 0.0
        h4_traffic = 0.0
        h3_conn = 0
        h4_conn = 0

        if isinstance(raw_data, dict):
            server_conns = raw_data.get('server_connections', {})
            server_traffics = raw_data.get('server_traffic', {})
            h3_conn = server_conns.get('h3', 0)
            h4_conn = server_conns.get('h4', 0)
            h3_traffic = server_traffics.get('h3', 0.0)
            h4_traffic = server_traffics.get('h4', 0.0)

        self.current_time += self.sample_interval
        self.time_history.pop(0)
        self.time_history.append(self.current_time)

        self.h3_traffic_history.pop(0)
        self.h3_traffic_history.append(h3_traffic)

        self.h4_traffic_history.pop(0)
        self.h4_traffic_history.append(h4_traffic)

        self.h3_conn_history.pop(0)
        self.h3_conn_history.append(h3_conn)

        self.h4_conn_history.pop(0)
        self.h4_conn_history.append(h4_conn)

        self.plot_traffic.setXRange(self.time_history[0], self.time_history[-1], padding=0)

        self.curve_h3_traffic.setData(self.time_history, self.h3_traffic_history)
        self.curve_h4_traffic.setData(self.time_history, self.h4_traffic_history)
        self.curve_h3_conn.setData(self.time_history, self.h3_conn_history)
        self.curve_h4_conn.setData(self.time_history, self.h4_conn_history)


# --- 5. 主視窗 ---
class MainApplication(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('SDN Real-Time Network Suite')
        self.setGeometry(100, 100, 1000, 800)

        self.create_menu_toolbar()

        self.stacked_widget = QStackedWidget()
        self.setCentralWidget(self.stacked_widget)

        self.topo_widget = TopologyWidget()
        self.chart_widget = ChartWidget()

        self.stacked_widget.addWidget(self.topo_widget)
        self.stacked_widget.addWidget(self.chart_widget)

        self.fetch_thread = DataFetchThread()
        self.fetch_thread.data_received.connect(self.dispatch_data)
        self.fetch_thread.start()

    def create_menu_toolbar(self):
        toolbar = QToolBar('Main Navigation')
        toolbar.setMovable(False)
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.addToolBar(toolbar)

        button_style = """
            QToolButton { 
                font-size: 13px; font-weight: bold; padding: 6px 20px; 
                background-color: #f3f4f6; border: 1px solid #d1d5db; border-radius: 4px; margin-right: 5px;
            }
            QToolButton:hover { background-color: #e5e7eb; }
            QToolButton:checked { background-color: #3498db; color: white; }
        """
        toolbar.setStyleSheet(button_style)

        action_topo = QAction('拓樸即時監控', self)
        action_topo.setCheckable(True)
        action_topo.setChecked(True)
        action_topo.triggered.connect(lambda: self.switch_view(0, action_topo))

        action_chart = QAction('負載與連線圖表', self)
        action_chart.setCheckable(True)
        action_chart.triggered.connect(lambda: self.switch_view(1, action_chart))

        toolbar.addAction(action_topo)
        toolbar.addAction(action_chart)

        self.nav_actions = [action_topo, action_chart]

    def switch_view(self, index, clicked_action):
        self.stacked_widget.setCurrentIndex(index)
        for act in self.nav_actions:
            act.setChecked(act == clicked_action)

    def dispatch_data(self, data):
        if not isinstance(data, dict):
            return

        links_list = data.get('links', [])
        server_conns = data.get('server_connections', {})
        server_traffic = data.get('server_traffic', {})
        server_load = data.get('server_load', {})

        self.topo_widget.update_ui(
            links_list,
            server_conns=server_conns,
            server_traffic=server_traffic,
            server_load=server_load
        )

        self.chart_widget.update_ui(data)


if __name__ == '__main__':
    app = QApplication(sys.argv)
    pg.setConfigOption('background', 'w')
    pg.setConfigOption('foreground', 'k')

    main_win = MainApplication()
    main_win.show()
    sys.exit(app.exec())
