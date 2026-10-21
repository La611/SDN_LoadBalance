from mininet.topo import Topo
from mininet.net import Mininet
from mininet.node import RemoteController, OVSKernelSwitch
from mininet.cli import CLI
from mininet.log import setLogLevel

class CustomTopology(Topo):
    def build(self):
        # 建立 Host
        h1 = self.addHost('h1', ip='172.16.0.1/24', defaultRoute='via 172.16.0.254')
        h2 = self.addHost('h2', ip='172.16.0.2/24', defaultRoute='via 172.16.0.254')
        h3 = self.addHost('h3', ip='192.168.0.10/24', mac='00:00:00:00:00:03', defaultRoute='via 192.168.0.254')
        h4 = self.addHost('h4', ip='192.168.0.20/24', mac='00:00:00:00:00:04', defaultRoute='via 192.168.0.254')

        # 建立 Switches
        s1 = self.addSwitch('s1') # 入口 1
        s2 = self.addSwitch('s2') # 入口 2
        s3 = self.addSwitch('s3') # 中間 1
        s4 = self.addSwitch('s4') # 中間 2
        s5 = self.addSwitch('s5') # 中間 3
        s6 = self.addSwitch('s6') # 擔當 NAT / 負載均衡器的 Switch

        # 建立 Links (Client -> Switch)
        self.addLink(h1, s1)
        self.addLink(h2, s2)

        # 建立 Links (Full Mesh 中間多路徑)
        for ingress in [s1, s2]:
            for mid in [s3, s4, s5]:
                self.addLink(ingress, mid)

        for mid in [s3, s4, s5]:
            self.addLink(mid, s6)

        # 建立 Links (NAT -> Server)
        self.addLink(s6, h3)
        self.addLink(s6, h4)

if __name__ == '__main__':
    setLogLevel('info')
    topo = CustomTopology()
    # 連接至外部 Ryu Controller (預設 127.0.0.1:6653)
    net = Mininet(topo=topo, switch=OVSKernelSwitch, controller=RemoteController)
    net.start()
    CLI(net)
    net.stop()
