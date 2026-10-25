"""Calibrate a configured cluster and apply reversible, peer-only WAN shaping."""

import base64
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import socket
import statistics
import uuid

from .cases import PAPER
from .configuration import remote_python

SOURCE = Path(__file__).with_name("network_node.py").read_text(encoding="utf-8")


class NetworkController:
    def __init__(self, config):
        self.config = config
        self.hosts = config["workers"]
        self.addresses = [socket.gethostbyname(host) for host in self.hosts]
        if len(set(self.addresses)) != 4:
            raise ValueError("Workers must have distinct IPv4 addresses; use isolated containers for local deployment")

    def worker(self, index, action, **values):
        own = self.addresses[index]
        payload = {"own": own, "peers": [ip for ip in self.addresses if ip != own], **values}
        encoded = base64.b64encode(json.dumps(payload).encode()).decode()
        process = remote_python(self.config, self.hosts[index], SOURCE, (action, encoded), timeout=1200 if action == "install" else 120)
        result = json.loads(process.stdout)
        if "error" in result:
            raise RuntimeError(result["error"])
        return result

    def inspect(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            return dict(zip(self.hosts, pool.map(lambda index: self.worker(index, "inspect"), range(4))))

    def ping_links(self):
        result = []
        for origin in range(4):
            for destination in range(4):
                if origin == destination:
                    continue
                record = self.worker(origin, "ping", peer=self.addresses[destination], count=5)
                match = re.search(r"=\s*([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)\s*ms", record["stdout"])
                if record["returncode"] or not match:
                    raise RuntimeError("Worker ping failed; verify the configured peer network")
                result.append({"from_party": origin, "to_party": destination, "rtt_ms": float(match[2])})
        return result

    def measure(self):
        latencies = self.ping_links()
        throughput = []
        for origin in range(4):
            for destination in range(4):
                if origin == destination:
                    continue
                token = "probe-" + uuid.uuid4().hex
                self.worker(destination, "server_start", token=token, port=5201)
                try:
                    record = self.worker(origin, "client", peer=self.addresses[destination], port=5201, seconds=3, omit=1)
                    if record["returncode"]:
                        raise RuntimeError("iperf3 failed; inspect worker connectivity")
                    report = json.loads(record["stdout"])
                    bps = report["end"]["sum_received"]["bits_per_second"]
                    throughput.append({"from_party": origin, "to_party": destination, "throughput_mbps": bps / 1_000_000})
                finally:
                    self.worker(destination, "server_stop", token=token)
        return {"latency": latencies, "throughput": throughput, "snapshot": self.inspect()}

    def require_condition(self, condition):
        snapshot = self.inspect()
        for host, entry in snapshot.items():
            state = entry.get("wan_state")
            if condition == "WAN":
                if not state or state.get("phase") != "applied_unvalidated" or state.get("bandwidth_mbps") != PAPER["wan"]["bandwidth_mbps"]:
                    raise ValueError(f"WAN rules are not configured on {host}; apply them or use --manage-network")
                if not any(q.get("handle") == "7a00:" for q in entry.get("qdiscs", [])):
                    raise ValueError("Saved WAN state does not match active traffic-control rules")
            elif state is not None:
                raise ValueError(f"Owned WAN shaping remains on {host}; restore it before LAN runs")
            elif any(q.get("kind") in ("netem", "htb") for q in entry.get("qdiscs", [])):
                raise ValueError("Unmanaged network shaping is present; inspect it before labeling a LAN run")
        return snapshot

    def set_condition(self, condition):
        if condition not in ("LAN", "WAN"):
            raise ValueError("Unknown network condition")
        with ThreadPoolExecutor(max_workers=4) as pool:
            restored = list(pool.map(lambda index: self.worker(index, "restore"), range(4)))
        if condition == "LAN":
            return {"condition": "LAN", "restored": restored, "snapshot": self.require_condition("LAN")}
        baseline = self.ping_links()
        native_rtt = statistics.median(item["rtt_ms"] for item in baseline)
        target_rtt = PAPER["wan"]["target_rtt_ms"]
        if native_rtt >= target_rtt:
            raise ValueError("The native network is already slower than the configured WAN target")
        delay = (target_rtt - native_rtt) / 2
        try:
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(lambda index: self.worker(index, "apply", delay_ms=delay), range(4)))
            measured = self.ping_links()
            if any(abs(item["rtt_ms"] - target_rtt) > max(5, target_rtt * 0.15) for item in measured):
                raise RuntimeError("Measured RTT does not match the WAN target")
            return {"condition": "WAN", "bandwidth_mbps": PAPER["wan"]["bandwidth_mbps"],
                    "target_rtt_ms": target_rtt, "native_rtt_ms": native_rtt,
                    "configured_one_way_delay_ms": delay, "latency": measured,
                    "snapshot": self.require_condition("WAN")}
        except Exception:
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(lambda index: self.worker(index, "restore"), range(4)))
            raise
