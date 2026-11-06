#!/usr/bin/env python3
"""Amostrador 1 Hz das UPFs. Roda no HOST e lê dentro de cada pod da UPF, via
'kubectl exec', os contadores de rede da interface do túnel (ogstun) e o uso de CPU
do cgroup. Acompanha pods novos (HPA) e removidos automaticamente.

Saídas (na pasta --out):
  upf_samples.csv  ts,pod,rx_bytes,tx_bytes,rx_pkts,tx_pkts,rx_drop,tx_drop,cpu_usec
  events.csv       ts,event,detail     (pod_present / pod_added / pod_removed)
  hpa.csv          ts,hpa,current,desired,cpu_pct   (se existir HPA da UPF)

Na ogstun do Open5GS: RX = tráfego do UE (uplink), TX = tráfego para o UE (downlink).
Encerre com SIGTERM/Ctrl+C.
"""
import argparse, csv, json, re, signal, subprocess, threading, time
from pathlib import Path

REMOTE = r"""
S=/sys/class/net/__IF__/statistics
while :; do
  read rxb < $S/rx_bytes; read txb < $S/tx_bytes
  read rxp < $S/rx_packets; read txp < $S/tx_packets
  read rxd < $S/rx_dropped; read txd < $S/tx_dropped
  cpu=0
  if [ -r /sys/fs/cgroup/cpu.stat ]; then
    while read k v; do [ "$k" = usage_usec ] && cpu=$v; done < /sys/fs/cgroup/cpu.stat
  elif [ -r /sys/fs/cgroup/cpuacct/cpuacct.usage ]; then
    read n < /sys/fs/cgroup/cpuacct/cpuacct.usage; cpu=$((n/1000))
  fi
  echo "$rxb $txb $rxp $txp $rxd $txd $cpu"
  sleep 1
done
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ns", required=True)
    ap.add_argument("--regex", default="upf")
    ap.add_argument("--iface", default="ogstun")
    ap.add_argument("--container", default="")
    ap.add_argument("--out", required=True)
    ap.add_argument("--poll", type=float, default=2.0)
    a = ap.parse_args()

    out = Path(a.out)
    rx = re.compile(a.regex, re.I)
    lock = threading.Lock()
    fs = open(out / "upf_samples.csv", "w", newline="")
    ws = csv.writer(fs)
    ws.writerow(["ts", "pod", "rx_bytes", "tx_bytes", "rx_pkts", "tx_pkts", "rx_drop", "tx_drop", "cpu_usec"])
    fe = open(out / "events.csv", "w", newline="")
    we = csv.writer(fe)
    we.writerow(["ts", "event", "detail"])
    fh = open(out / "hpa.csv", "w", newline="")
    wh = csv.writer(fh)
    wh.writerow(["ts", "hpa", "current", "desired", "cpu_pct"])
    fs.flush(); fe.flush(); fh.flush()

    stop = threading.Event()
    for s in (signal.SIGINT, signal.SIGTERM):
        signal.signal(s, lambda *_: stop.set())
    remote = REMOTE.replace("__IF__", a.iface)
    workers = {}

    def event(kind, detail=""):
        with lock:
            we.writerow([f"{time.time():.3f}", kind, detail])
            fe.flush()

    def reader(pod, proc):
        for line in proc.stdout:
            p = line.split()
            if len(p) != 7:
                continue
            try:
                vals = [int(x) for x in p]
            except ValueError:
                continue
            with lock:
                ws.writerow([f"{time.time():.3f}", pod, *vals])
                fs.flush()

    def attach(pod):
        cmd = ["kubectl", "-n", a.ns, "exec", pod]
        if a.container:
            cmd += ["-c", a.container]
        cmd += ["--", "sh", "-c", remote]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
        t = threading.Thread(target=reader, args=(pod, proc), daemon=True)
        t.start()
        workers[pod] = (proc, t, time.time())

    def list_pods():
        r = subprocess.run(["kubectl", "-n", a.ns, "get", "pods", "-o", "json"], capture_output=True, text=True)
        if r.returncode:
            return None
        try:
            items = json.loads(r.stdout)["items"]
        except Exception:
            return None
        pods = []
        for it in items:
            n = it["metadata"]["name"]
            if rx.search(n) and not it["metadata"].get("deletionTimestamp") \
                    and it.get("status", {}).get("phase") == "Running":
                pods.append(n)
        return pods

    def poll_hpa():
        r = subprocess.run(["kubectl", "-n", a.ns, "get", "hpa", "-o", "json"], capture_output=True, text=True)
        if r.returncode:
            return
        try:
            items = json.loads(r.stdout)["items"]
        except Exception:
            return
        for it in items:
            tgt = it.get("spec", {}).get("scaleTargetRef", {}).get("name", "")
            if not (rx.search(tgt) or rx.search(it["metadata"]["name"])):
                continue
            st = it.get("status", {})
            cpu = ""
            for m in st.get("currentMetrics") or []:
                v = ((m.get("resource") or {}).get("current") or {}).get("averageUtilization")
                if v is not None:
                    cpu = v
                    break
            wh.writerow([f"{time.time():.3f}", it["metadata"]["name"],
                         st.get("currentReplicas", ""), st.get("desiredReplicas", ""), cpu])
        fh.flush()

    known, first = set(), True
    while not stop.is_set():
        pods = list_pods()
        if pods is not None:
            for p in pods:
                if p not in known:
                    known.add(p)
                    event("pod_present" if first else "pod_added", p)
                    attach(p)
                else:
                    proc, _, t0 = workers[p]
                    if proc.poll() is not None and time.time() - t0 > 5:
                        attach(p)
            for p in list(known - set(pods)):
                known.discard(p)
                event("pod_removed", p)
                proc, _, _ = workers.pop(p)
                proc.terminate()
            first = False
        poll_hpa()
        stop.wait(a.poll)

    for proc, _, _ in workers.values():
        proc.terminate()
    time.sleep(0.3)
    for f in (fs, fe, fh):
        f.close()


if __name__ == "__main__":
    main()
