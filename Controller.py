import json
import time
import networkx as nx

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, ipv4, arp, tcp, ether_types
from ryu.lib import hub

# REST API WSGI
from ryu.app.wsgi import ControllerBase, WSGIApplication, route
from webob import Response


STATS_URL = '/stats/links'

# ============================================================
# Load Balancer 參數
# ============================================================
CONN_WEIGHT = 0.6
TRAFFIC_WEIGHT = 0.4

MAX_EXPECTED_CONNECTIONS = 100.0
MAX_EXPECTED_TRAFFIC_KBPS = 10 * 1024.0   # 10 MB/s

CONNECTION_IDLE_TIMEOUT = 300


class StatsController(ControllerBase):
    def __init__(self, req, link, data, **config):
        super(StatsController, self).__init__(req, link, data, **config)
        self.app = data['app']

    @route('topology', STATS_URL, methods=['GET'])
    def get_stats(self, req, **kwargs):
        body = json.dumps(self.app.get_ui_data())
        return Response(
            content_type='application/json',
            charset='utf-8',
            body=body
        )


class SDNLoadBalancerNAT(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
    _CONTEXTS = {'wsgi': WSGIApplication}

    def __init__(self, *args, **kwargs):
        super(SDNLoadBalancerNAT, self).__init__(*args, **kwargs)

        self.datapaths = {}
        self.net = nx.DiGraph()

        # REST API
        wsgi = kwargs['wsgi']
        wsgi.register(StatsController, {'app': self})

        # ====================================================
        # NAT / VIP / Server 設定
        # ====================================================
        self.VIP = '172.16.0.254'
        self.VIP_MAC = '00:00:00:00:02:54'

        self.client_mac_table = {}

        self.SERVERS = {
            'h3': {
                'ip': '192.168.0.10',
                'mac': '00:00:00:00:00:03',
                'port': 4,
                'bytes': 0,
                'conns': 0,
                'rate': 0.0,
                'conn_score': 0.0,
                'traffic_score': 0.0,
                'load_score': 0.0,
            },
            'h4': {
                'ip': '192.168.0.20',
                'mac': '00:00:00:00:00:04',
                'port': 5,
                'bytes': 0,
                'conns': 0,
                'rate': 0.0,
                'conn_score': 0.0,
                'traffic_score': 0.0,
                'load_score': 0.0,
            }
        }

        # TCP Connection Tracking
        self.connections = {}
        self.next_connection_id = 1

        # Switch port statistics
        self.port_stats = {}
        self.link_rates = {}

        # ====================================================
        # 中間層動態節能/擴容參數 (單位: KB/s)
        # ====================================================
        self.active_mid_switches = [3]  # 初始只啟用 s3
        self.current_mid_traffic_kbps = 0.0

        # 追蹤各中間 switch 活躍連線
        self.mid_active_conns = {3: 0, 4: 0, 5: 0}
        self.flow_to_mid_map = {}

        # 擴容門檻 (Scale-up)
        self.SCALE_UP_THRESHOLDS = {
            1: 2000.0,  # 總流量 > 2000 KB/s 時開啟 s4
            2: 5000.0,  # 總流量 > 5000 KB/s 時開啟 s5
        }
        # 縮容門檻 (Scale-down)
        self.SCALE_DOWN_THRESHOLDS = {
            3: 4000.0,  # 總流量 < 4000 KB/s 時關閉 s5
            2: 1200.0,  # 總流量 < 1200 KB/s 時關閉 s4
        }

        self.setup_topology_graph()
        self.monitor_thread = hub.spawn(self._monitor)

    def setup_topology_graph(self):
        for dpid in range(1, 7):
            self.net.add_node(dpid)

        self.links = [
            (1, 3, 2, 1),
            (1, 4, 3, 1),
            (1, 5, 4, 1),
            (2, 3, 2, 2),
            (2, 4, 3, 2),
            (2, 5, 4, 2),
            (3, 6, 3, 1),
            (4, 6, 3, 2),
            (5, 6, 3, 3),
        ]

        for u, v, u_port, v_port in self.links:
            self.net.add_edge(u, v, port=u_port, weight=1)
            self.net.add_edge(v, u, port=v_port, weight=1)

    def get_least_loaded_mid_port(self, dpid, src_ip, src_port):
        """
        在【當前已開放】的中間交換機中動態挑選負載最小的路徑
        """
        dpid_to_port = {3: 2, 4: 3, 5: 4}
        available_switches = [s for s in self.active_mid_switches if s in dpid_to_port]

        if not available_switches:
            return 2

        if len(available_switches) == 1:
            best_mid = available_switches[0]
        else:
            def calculate_load(mid_sw):
                port = dpid_to_port[mid_sw]
                stat = self.port_stats.get((dpid, port), {})
                rate = stat.get('rate', 0.0)
                conns = self.mid_active_conns.get(mid_sw, 0)
                tx_bytes = stat.get('tx_bytes', 0)
                # 綜合負載：實際速率 + 正在該交換機上的連線數權重
                effective_load = rate + conns * 500.0
                return (effective_load, conns, tx_bytes)

            best_mid = min(available_switches, key=calculate_load)

        # 登記連線指派
        flow_key = (src_ip, src_port)
        if flow_key not in self.flow_to_mid_map:
            self.flow_to_mid_map[flow_key] = best_mid
            self.mid_active_conns[best_mid] = self.mid_active_conns.get(best_mid, 0) + 1

        selected_port = dpid_to_port[best_mid]
        print(f"[PATH SELECTION] Client {src_ip}:{src_port} -> s{dpid} Port {selected_port} (Route via s{best_mid}) | "
              f"Active Switches: {self.active_mid_switches}, Mid-Conns: {self.mid_active_conns}")
        return selected_port

    def check_and_scale_mid_switches(self, traffic_rate):
        """依據 Client 總流量調整中間交換機開關狀態"""
        current_count = len(self.active_mid_switches)
        new_active = list(self.active_mid_switches)

        # 擴容判定
        if current_count == 1 and traffic_rate > self.SCALE_UP_THRESHOLDS[1]:
            new_active = [3, 4]
        elif current_count == 2 and traffic_rate > self.SCALE_UP_THRESHOLDS[2]:
            new_active = [3, 4, 5]

        # 縮容判定
        elif current_count == 3 and traffic_rate < self.SCALE_DOWN_THRESHOLDS[3]:
            new_active = [3, 4]
        elif current_count == 2 and traffic_rate < self.SCALE_DOWN_THRESHOLDS[2]:
            new_active = [3]

        if set(new_active) != set(self.active_mid_switches):
            print(f"[ELASTIC SCALING] Traffic: {traffic_rate:.2f} KB/s. "
                  f"Mid Switches changed: {self.active_mid_switches} -> {new_active}")
            self.active_mid_switches = new_active
            # 注意：這裡不主動中斷既有連線，讓既有 iperf 繼續流動，新連線自動進新 switch

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        self.datapaths[datapath.id] = datapath

        match = parser.OFPMatch()
        actions = [
            parser.OFPActionOutput(
                ofproto.OFPP_CONTROLLER,
                ofproto.OFPCML_NO_BUFFER
            )
        ]
        self.add_flow(datapath, 0, match, actions)

    def add_flow(self, datapath, priority, match, actions, idle=0, hard=0, cookie=0, send_flow_removed=False):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        inst = [
            parser.OFPInstructionActions(
                ofproto.OFPIT_APPLY_ACTIONS,
                actions
            )
        ]

        flags = 0
        if send_flow_removed:
            flags |= ofproto.OFPFF_SEND_FLOW_REM

        mod = parser.OFPFlowMod(
            datapath=datapath,
            priority=priority,
            match=match,
            instructions=inst,
            idle_timeout=idle,
            hard_timeout=hard,
            cookie=cookie,
            flags=flags,
        )
        datapath.send_msg(mod)

    @set_ev_cls(ofp_event.EventOFPFlowRemoved, MAIN_DISPATCHER)
    def flow_removed_handler(self, ev):
        """連線結束超時移除時，釋放該中間交換機的負載計數"""
        match = ev.msg.match
        src_ip = match.get('ipv4_src')
        tcp_src = match.get('tcp_src')
        if src_ip and tcp_src:
            key = (src_ip, tcp_src)
            mid = self.flow_to_mid_map.pop(key, None)
            if mid and mid in self.mid_active_conns:
                self.mid_active_conns[mid] = max(0, self.mid_active_conns[mid] - 1)

    def _monitor(self):
        while True:
            hub.sleep(2)
            for dp in self.datapaths.values():
                parser = dp.ofproto_parser
                req_port = parser.OFPPortStatsRequest(dp, 0, dp.ofproto.OFPP_ANY)
                dp.send_msg(req_port)

                if dp.id == 6:
                    req_flow = parser.OFPFlowStatsRequest(dp)
                    dp.send_msg(req_flow)

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def port_stats_reply_handler(self, ev):
        dpid = ev.msg.datapath.id
        now = time.time()

        for stat in ev.msg.body:
            port_no = stat.port_no
            key = (dpid, port_no)

            tx_bytes = stat.tx_bytes
            rx_bytes = stat.rx_bytes

            if key in self.port_stats:
                prev = self.port_stats[key]
                delta_t = now - prev['timestamp']

                if delta_t > 0:
                    tx_delta = max(0, tx_bytes - prev['tx_bytes'])
                    rx_delta = max(0, rx_bytes - prev['rx_bytes'])
                    rate = ((tx_delta + rx_delta) / 1024.0) / delta_t
                    self.port_stats[key]['rate'] = round(rate, 2)
            else:
                self.port_stats[key] = {
                    'tx_bytes': tx_bytes,
                    'rx_bytes': rx_bytes,
                    'timestamp': now,
                    'rate': 0.0,
                }

            self.port_stats[key]['tx_bytes'] = tx_bytes
            self.port_stats[key]['rx_bytes'] = rx_bytes
            self.port_stats[key]['timestamp'] = now

        # 計算 Ingress 總流量 (s1 port 1 + s2 port 1) 並觸發中間交換機伸縮
        r_c1 = self.port_stats.get((1, 1), {}).get('rate', 0.0)
        r_c2 = self.port_stats.get((2, 1), {}).get('rate', 0.0)
        self.current_mid_traffic_kbps = r_c1 + r_c2
        self.check_and_scale_mid_switches(self.current_mid_traffic_kbps)

        if dpid == 6:
            for s_name, s_info in self.SERVERS.items():
                key = (6, s_info['port'])
                p = self.port_stats.get(key, {})
                s_info['rate'] = p.get('rate', 0.0)

        self.update_load_scores()

    def update_load_scores(self):
        for s_name, s_info in self.SERVERS.items():
            conn_score = min(s_info['conns'] / MAX_EXPECTED_CONNECTIONS, 1.0)
            traffic_score = min(s_info['rate'] / MAX_EXPECTED_TRAFFIC_KBPS, 1.0)
            load_score = CONN_WEIGHT * conn_score + TRAFFIC_WEIGHT * traffic_score

            s_info['conn_score'] = round(conn_score, 4)
            s_info['traffic_score'] = round(traffic_score, 4)
            s_info['load_score'] = round(load_score, 4)

    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def flow_stats_reply_handler(self, ev):
        dpid = ev.msg.datapath.id
        if dpid != 6:
            return

        active_connection_keys = set()
        server_conn_count = {name: 0 for name in self.SERVERS}

        for stat in ev.msg.body:
            m = stat.match
            try:
                eth_type = m.get('eth_type')
                ip_proto = m.get('ip_proto')
                ipv4_src = m.get('ipv4_src')
                ipv4_dst = m.get('ipv4_dst')
                tcp_src = m.get('tcp_src')
                tcp_dst = m.get('tcp_dst')
            except Exception:
                continue

            if eth_type != ether_types.ETH_TYPE_IP or ip_proto != 6:
                continue
            if ipv4_dst != self.VIP or tcp_dst != 80:
                continue
            if ipv4_src is None or tcp_src is None:
                continue

            key = (ipv4_src, int(tcp_src), self.VIP, 80)
            active_connection_keys.add(key)

            conn = self.connections.get(key)
            if conn:
                conn['last_bytes'] = stat.byte_count
                conn['last_seen'] = time.time()
                server_conn_count[conn['server']] += 1

        stale_keys = [k for k in self.connections if k not in active_connection_keys]
        for key in stale_keys:
            del self.connections[key]

        for s_name in self.SERVERS:
            self.SERVERS[s_name]['conns'] = server_conn_count[s_name]

        self.update_load_scores()

    def get_least_loaded_server(self):
        self.update_load_scores()
        best_server = min(self.SERVERS.items(), key=lambda item: item[1]['load_score'])
        return best_server[0], best_server[1]

    def install_tcp_connection_flows(self, datapath, src_ip, src_port, dst_ip, dst_port, server_name, server):
        parser = datapath.ofproto_parser

        # DNAT
        match_dnat = parser.OFPMatch(
            eth_type=ether_types.ETH_TYPE_IP,
            ipv4_src=src_ip,
            ipv4_dst=self.VIP,
            ip_proto=6,
            tcp_src=src_port,
            tcp_dst=dst_port,
        )
        actions_dnat = [
            parser.OFPActionSetField(eth_src=self.VIP_MAC),
            parser.OFPActionSetField(eth_dst=server['mac']),
            parser.OFPActionSetField(ipv4_dst=server['ip']),
            parser.OFPActionOutput(server['port']),
        ]
        self.add_flow(datapath, 200, match_dnat, actions_dnat, idle=CONNECTION_IDLE_TIMEOUT)

        # SNAT
        real_client_mac = self.client_mac_table.get(src_ip, 'ff:ff:ff:ff:ff:ff')
        out_port = 1 if src_ip == '172.16.0.1' else 2

        match_snat = parser.OFPMatch(
            eth_type=ether_types.ETH_TYPE_IP,
            ipv4_src=server['ip'],
            ipv4_dst=src_ip,
            ip_proto=6,
            tcp_src=dst_port,
            tcp_dst=src_port,
        )
        actions_snat = [
            parser.OFPActionSetField(eth_src=self.VIP_MAC),
            parser.OFPActionSetField(eth_dst=real_client_mac),
            parser.OFPActionSetField(ipv4_src=self.VIP),
            parser.OFPActionOutput(out_port),
        ]
        self.add_flow(datapath, 200, match_snat, actions_snat, idle=CONNECTION_IDLE_TIMEOUT)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        in_port = msg.match['in_port']
        dpid = datapath.id

        pkt = packet.Packet(msg.data)
        eth_pkt = pkt.get_protocol(ethernet.ethernet)
        arp_pkt = pkt.get_protocol(arp.arp)
        ip_pkt = pkt.get_protocol(ipv4.ipv4)
        tcp_pkt = pkt.get_protocol(tcp.tcp)

        if eth_pkt and ip_pkt and ip_pkt.src.startswith('172.16.0.'):
            self.client_mac_table[ip_pkt.src] = eth_pkt.src
        elif eth_pkt and arp_pkt and arp_pkt.src_ip.startswith('172.16.0.'):
            self.client_mac_table[arp_pkt.src_ip] = arp_pkt.src_mac

        # ARP 處理
        if arp_pkt:
            if arp_pkt.dst_ip in [self.VIP, '192.168.0.254']:
                if arp_pkt.opcode == arp.ARP_REQUEST:
                    self.handle_arp_reply(datapath, in_port, arp_pkt)
                return

            target_port = 1
            if dpid in [1, 2]:
                target_port = 1 if arp_pkt.dst_ip in ['172.16.0.1', '172.16.0.2'] else 2
            elif dpid in [3, 4, 5]:
                if arp_pkt.dst_ip == '172.16.0.1':
                    target_port = 1
                elif arp_pkt.dst_ip == '172.16.0.2':
                    target_port = 2
                else:
                    target_port = 3
            elif dpid == 6:
                if arp_pkt.dst_ip == '192.168.0.10':
                    target_port = 4
                elif arp_pkt.dst_ip == '192.168.0.20':
                    target_port = 5
                else:
                    return

            actions = [parser.OFPActionOutput(target_port)]
            out = parser.OFPPacketOut(
                datapath=datapath,
                buffer_id=msg.buffer_id,
                in_port=in_port,
                actions=actions,
                data=msg.data if msg.buffer_id == ofproto.OFP_NO_BUFFER else None
            )
            datapath.send_msg(out)
            return

        # IP 處理
        if ip_pkt:
            src_ip = ip_pkt.src
            dst_ip = ip_pkt.dst

            # s6: LB & NAT
            if dpid == 6:
                if dst_ip == self.VIP and tcp_pkt and tcp_pkt.dst_port == 80:
                    src_port = tcp_pkt.src_port
                    dst_port = tcp_pkt.dst_port
                    connection_key = (src_ip, src_port, self.VIP, dst_port)

                    existing = self.connections.get(connection_key)
                    if existing:
                        server_name = existing['server']
                        server = self.SERVERS[server_name]
                    else:
                        flags = tcp_pkt.bits
                        is_syn = bool(flags & 0x02)
                        is_ack = bool(flags & 0x10)

                        if not is_syn or is_ack:
                            return

                        server_name, server = self.get_least_loaded_server()
                        connection_id = self.next_connection_id
                        self.next_connection_id += 1

                        self.connections[connection_key] = {
                            'server': server_name,
                            'connection_id': connection_id,
                            'created': time.time(),
                            'last_seen': time.time(),
                            'last_bytes': 0,
                        }
                        server['conns'] += 1
                        self.update_load_scores()

                        print(f'[LB] NEW TCP #{connection_id}: {src_ip}:{src_port} -> VIP:{dst_port} -> {server_name}')

                    self.install_tcp_connection_flows(
                        datapath, src_ip, src_port, dst_ip, dst_port, server_name, server
                    )

                    actions_dnat = [
                        parser.OFPActionSetField(eth_src=self.VIP_MAC),
                        parser.OFPActionSetField(eth_dst=server['mac']),
                        parser.OFPActionSetField(ipv4_dst=server['ip']),
                        parser.OFPActionOutput(server['port']),
                    ]
                    out = parser.OFPPacketOut(
                        datapath=datapath,
                        buffer_id=msg.buffer_id,
                        in_port=in_port,
                        actions=actions_dnat,
                        data=msg.data if msg.buffer_id == ofproto.OFP_NO_BUFFER else None
                    )
                    datapath.send_msg(out)
                    return

                elif src_ip in ['192.168.0.10', '192.168.0.20'] and tcp_pkt:
                    server_name = 'h3' if src_ip == '192.168.0.10' else 'h4'
                    server = self.SERVERS[server_name]
                    real_client_mac = self.client_mac_table.get(dst_ip, 'ff:ff:ff:ff:ff:ff')
                    out_port = 1 if dst_ip == '172.16.0.1' else 2

                    match_snat = parser.OFPMatch(
                        eth_type=ether_types.ETH_TYPE_IP,
                        ipv4_src=server['ip'],
                        ipv4_dst=dst_ip,
                        ip_proto=6,
                        tcp_src=tcp_pkt.src_port,
                        tcp_dst=tcp_pkt.dst_port,
                    )
                    actions_snat = [
                        parser.OFPActionSetField(eth_src=self.VIP_MAC),
                        parser.OFPActionSetField(eth_dst=real_client_mac),
                        parser.OFPActionSetField(ipv4_src=self.VIP),
                        parser.OFPActionOutput(out_port),
                    ]
                    self.add_flow(datapath, 200, match_snat, actions_snat, idle=CONNECTION_IDLE_TIMEOUT)

                    out = parser.OFPPacketOut(
                        datapath=datapath,
                        buffer_id=msg.buffer_id,
                        in_port=in_port,
                        actions=actions_snat,
                        data=msg.data if msg.buffer_id == ofproto.OFP_NO_BUFFER else None
                    )
                    datapath.send_msg(out)
                    return

            # s3, s4, s5 回程轉發
            elif dpid in [3, 4, 5]:
                out_port = 1 if dst_ip == '172.16.0.1' else (2 if dst_ip == '172.16.0.2' else 3)
                match = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP, ipv4_dst=dst_ip)
                actions = [parser.OFPActionOutput(out_port)]
                self.add_flow(datapath, 20, match, actions, idle=30)

                out = parser.OFPPacketOut(
                    datapath=datapath,
                    buffer_id=msg.buffer_id,
                    in_port=in_port,
                    actions=actions,
                    data=msg.data if msg.buffer_id == ofproto.OFP_NO_BUFFER else None
                )
                datapath.send_msg(out)
                return

            # s1, s2 轉發 (往中間層與往 Client)
            elif dpid in [1, 2]:
                if (dpid == 1 and dst_ip == '172.16.0.1') or (dpid == 2 and dst_ip == '172.16.0.2'):
                    # Server -> Client
                    out_port = 1
                    priority = 200
                    match = parser.OFPMatch(
                        eth_type=ether_types.ETH_TYPE_IP,
                        ipv4_src=self.VIP,
                        ipv4_dst=dst_ip
                    )
                    actions = [parser.OFPActionOutput(out_port)]
                    self.add_flow(datapath, priority, match, actions, idle=30)
                else:
                    # Client -> VIP: 每次新進來的 iperf 連線動態選擇當前可用中負載最小的 Switch
                    if tcp_pkt:
                        src_port = tcp_pkt.src_port
                        out_port = self.get_least_loaded_mid_port(dpid, src_ip, src_port)
                        priority = 100
                        match = parser.OFPMatch(
                            eth_type=ether_types.ETH_TYPE_IP,
                            ip_proto=6,
                            ipv4_src=src_ip,
                            ipv4_dst=self.VIP,
                            tcp_src=src_port,
                            tcp_dst=tcp_pkt.dst_port
                        )
                        actions = [parser.OFPActionOutput(out_port)]
                        self.add_flow(datapath, priority, match, actions, idle=30, send_flow_removed=True)
                    else:
                        dpid_to_port = {3: 2, 4: 3, 5: 4}
                        out_port = dpid_to_port[min(self.active_mid_switches)]
                        priority = 50
                        match = parser.OFPMatch(
                            eth_type=ether_types.ETH_TYPE_IP,
                            ipv4_src=src_ip,
                            ipv4_dst=self.VIP
                        )
                        actions = [parser.OFPActionOutput(out_port)]
                        self.add_flow(datapath, priority, match, actions, idle=30)

                out = parser.OFPPacketOut(
                    datapath=datapath,
                    buffer_id=msg.buffer_id,
                    in_port=in_port,
                    actions=actions,
                    data=msg.data if msg.buffer_id == ofproto.OFP_NO_BUFFER else None
                )
                datapath.send_msg(out)
                return

    def handle_arp_reply(self, datapath, in_port, arp_pkt):
        pkt = packet.Packet()
        pkt.add_protocol(
            ethernet.ethernet(
                ethertype=ether_types.ETH_TYPE_ARP,
                dst=arp_pkt.src_mac,
                src=self.VIP_MAC,
            )
        )
        pkt.add_protocol(
            arp.arp(
                opcode=arp.ARP_REPLY,
                src_mac=self.VIP_MAC,
                src_ip=arp_pkt.dst_ip,
                dst_mac=arp_pkt.src_mac,
                dst_ip=arp_pkt.src_ip,
            )
        )
        pkt.serialize()

        parser = datapath.ofproto_parser
        actions = [parser.OFPActionOutput(in_port)]
        out = parser.OFPPacketOut(
            datapath=datapath,
            buffer_id=datapath.ofproto.OFP_NO_BUFFER,
            in_port=datapath.ofproto.OFPP_CONTROLLER,
            actions=actions,
            data=pkt.data,
        )
        datapath.send_msg(out)

    def get_ui_data(self):
        self.update_load_scores()
        links_data = []

        links_data.append({'source': 'h1', 'target': 's1', 'rate': self.port_stats.get((1, 1), {}).get('rate', 0.0)})
        links_data.append({'source': 'h2', 'target': 's2', 'rate': self.port_stats.get((2, 1), {}).get('rate', 0.0)})
        links_data.append({'source': 's6', 'target': 'h3', 'rate': self.port_stats.get((6, 4), {}).get('rate', 0.0)})
        links_data.append({'source': 's6', 'target': 'h4', 'rate': self.port_stats.get((6, 5), {}).get('rate', 0.0)})

        for u, v, u_port, v_port in self.links:
            r1 = self.port_stats.get((u, u_port), {}).get('rate', 0.0)
            r2 = self.port_stats.get((v, v_port), {}).get('rate', 0.0)
            links_data.append({
                'source': f's{u}',
                'target': f's{v}',
                'rate': round((r1 + r2) / 2.0, 2),
            })

        return {
            'links': links_data,
            'server_connections': {s: info['conns'] for s, info in self.SERVERS.items()},
            'server_traffic': {s: info['rate'] for s, info in self.SERVERS.items()},
            'server_load': {
                s: {
                    'connections': info['conns'],
                    'traffic_kbps': info['rate'],
                    'conn_score': info['conn_score'],
                    'traffic_score': info['traffic_score'],
                    'load_score': info['load_score'],
                }
                for s, info in self.SERVERS.items()
            },
            'active_mid_switches': [f's{x}' for x in self.active_mid_switches],
            'current_mid_traffic_kbps': round(self.current_mid_traffic_kbps, 2),
            'weights': {'connections': CONN_WEIGHT, 'traffic': TRAFFIC_WEIGHT},
            'is_blocked': False,
        }
