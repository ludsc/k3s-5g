#!/usr/bin/env python3
"""Amostrador 1 Hz (roda no HOST, lê dentro dos pods via 'kubectl exec').

UPF  : contadores de rede da interface do túnel (ogstun), CPU e throttling (limite de CPU) do cgroup.
Extras (--extra papel=regex, ex.: ue=..., gnb=...): só CPU e throttling do cgroup de cada pod.
Acompanha pods novos (HPA) e removidos automaticamente.

Saídas (na pasta --out):
  upf_samples.csv  ts,pod,rx_bytes,tx_bytes,rx_pkts,tx_pkts,rx_drop,tx_drop,cpu_usec,thr_usec,nr_thr
  pods_cpu.csv     ts,role,pod,cpu_usec,thr_usec,nr_thr      (UE, gNB, ...)
  events.csv       ts,event,detail     (pod_present / pod_added / pod_removed; extras: <papel>_pod_*)
  hpa.csv          ts,hpa,current,desired,cpu_pct            (se existir HPA da UPF)

Na ogstun do Open5GS: RX = tráfego do UE (uplink), TX = tráfego para o UE (downlink).
thr_usec = tempo acumulado em que o cgroup ficou estrangulado pelo limite de CPU (cgroup v2: cpu.stat).
Encerre com SIGTERM/Ctrl+C.
"""
import argparse, csv, json, re, signal, subprocess, threading, time
from pathlib import Path

# Define cpu (usec acumulados), thr (usec estrangulados) e nthr (nº de períodos estrangulados)
CPU_SH = r"""
cpu=0; thr=0; nthr=0
if [ -r /sys/fs/cgroup/cpu.stat ]; then
  while read k v; do
    case $k in usage_usec) cpu=$v;; throttled_usec) thr=$v;; nr_throttled) nthr=$v;; esac
  done < /sys/fs/cgroup/cpu.stat
else
  if [ -r /sys/fs/cgroup/cpuacct/cpuacct.usage ]; then read n < /sys/fs/cgroup/cpuacct/cpuacct.usage; cpu=$((n/1000)); fi
  if [ -r /sys/fs/cgroup/cpu/cpu.stat ]; then
    while read k v; do
      case $k in nr_throttled) nthr=$v;; throttled_time) thr=$((v/1000));; esac
    done < /sys/fs/cgroup/cpu/cpu.stat
  fi
fi
"""

NET_SH = r"""
read rxb < $S/rx_bytes; read txb < $S/tx_bytes
read rxp < $S/rx_packets; read txp < $S/tx_packets
read rxd < $S/rx_dropped; read txd < $S/tx_dropped
"""

REMOTE_UPF = ("S=/sys/class/net/__IF__/statistics\nwhile :; do\n" + NET_SH + CPU_SH +
              'echo "$rxb $txb $rxp $txp $rxd $txd $cpu $thr $nthr"\nsleep 1\ndone\n')
REMOTE_CPU = "while :; do\n" + CPU_SH + 'echo "$cpu $thr $nthr"\nsleep 1\ndone\n'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ns", required=True)
    ap.add_argument("--regex", default="upf")
    ap.add_argument("--iface", default="ogstun")
    ap.add_argument("--container", default="")
    ap.add_argument("--extra", action="append", default=[], help="papel=regex (só CPU), repetível")
    ap.add_argument("--extra-ns", default="")
    ap.add_argument("--out", required=True)
    ap.add_argument("--poll", type=float, default=2.0)
    a = ap.parse_args()

    out = Path(a.out)
    lock = threading.Lock()
    fs = open(out / "upf_samples.csv", "w", newline="")
    ws = csv.writer(fs)
    ws.writerow(["ts", "pod", "rx_bytes", "tx_bytes", "rx_pkts", "tx_pkts", "rx_drop", "tx_drop",
                 "cpu_usec", "thr_usec", "nr_thr"])
    fc = open(out / "pods_cpu.csv", "w", newline="")
    wc = csv.writer(fc)
    wc.writerow(["ts", "role", "pod", "cpu_usec", "thr_usec", "nr_thr"])
    fe = open(out / "events.csv", "w", newline="")
    we = csv.writer(fe)
    we.writerow(["ts", "event", "detail"])
    fh = open(out / "hpa.csv", "w", newline="")
    wh = csv.writer(fh)
    wh.writerow(["ts", "hpa", "current", "desired", "cpu_pct"])
    for f in (fs, fc, fe, fh):
        f.flush()

    stop = threading.Event()
    for s in (signal.SIGINT, signal.SIGTERM):
        signal.signal(s, lambda *_: stop.set())

    groups = [dict(role="upf", ns=a.ns, rx=re.compile(a.regex, re.I), script=REMOTE_UPF.replace("__IF__", a.iface),
                   container=a.container, nvals=9, known=set(), first=True)]
    for spec in a.extra:
        role, _, rgx = spec.partition("=")
        groups.append(dict(role=role, ns=a.extra_ns or a.ns, rx=re.compile(rgx, re.I), script=REMOTE_CPU,
                           container="", nvals=3, known=set(), first=True))
    workers = {}

    def event(kind, detail=""):
        with lock:
            we.writerow([f"{time.time():.3f}", kind, detail])
            fe.flush()

    def reader(pod, proc, g):
        for line in proc.stdout:
            p = line.split()
            if len(p) != g["nvals"]:
                continue
            try:
                vals = [int(x) for x in p]
            except ValueError:
                continue
            with lock:
                if g["role"] == "upf":
                    ws.writerow([f"{time.time():.3f}", pod, *vals]); fs.flush()
                else:
                    wc.writerow([f"{time.time():.3f}", g["role"], pod, *vals]); fc.flush()

    def attach(pod, g):
        cmd = ["kubectl", "-n", g["ns"], "exec", pod]
        if g["container"]:
            cmd += ["-c", g["container"]]
        cmd += ["--", "sh", "-c", g["script"]]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
        t = threading.Thread(target=reader, args=(pod, proc, g), daemon=True)
        t.start()
        workers[(g["role"], pod)] = (proc, t, time.time())

    def list_pods(ns, rx, cache):
        if ns not in cache:
            r = subprocess.run(["kubectl", "-n", ns, "get", "pods", "-o", "json"], capture_output=True, text=True)
            try:
                cache[ns] = json.loads(r.stdout)["items"] if r.returncode == 0 else None
            except Exception:
                cache[ns] = None
        if cache[ns] is None:
            return None
        pods = []
        for it in cache[ns]:
            n = it["metadata"]["name"]
            if rx.search(n) and not it["metadata"].get("deletionTimestamp") \
                    and it.get("status", {}).get("phase") == "Running":
                pods.append(n)
        return pods

    def poll_hpa():
        g = groups[0]
        r = subprocess.run(["kubectl", "-n", g["ns"], "get", "hpa", "-o", "json"], capture_output=True, text=True)
        if r.returncode:
            return
        try:
            items = json.loads(r.stdout)["items"]
        except Exception:
            return
        for it in items:
            tgt = it.get("spec", {}).get("scaleTargetRef", {}).get("name", "")
            if not (g["rx"].search(tgt) or g["rx"].search(it["metadata"]["name"])):
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

    n_loop = 0
    while not stop.is_set():
        cache = {}
        for g in groups:
            pods = list_pods(g["ns"], g["rx"], cache)
            if pods is None:
                continue
            pre = "" if g["role"] == "upf" else g["role"] + "_"
            for p in pods:
                key = (g["role"], p)
                if p not in g["known"]:
                    g["known"].add(p)
                    event(pre + ("pod_present" if g["first"] else "pod_added"), p)
                    attach(p, g)
                else:
                    proc, _, t0 = workers[key]
                    if proc.poll() is not None and time.time() - t0 > 5:
                        attach(p, g)
            for p in list(g["known"] - set(pods)):
                g["known"].discard(p)
                event(pre + "pod_removed", p)
                proc, _, _ = workers.pop((g["role"], p))
                proc.terminate()
            g["first"] = False
        if n_loop % 5 == 0:
            poll_hpa()
        n_loop += 1
        stop.wait(a.poll)

    for proc, _, _ in workers.values():
        proc.terminate()
    time.sleep(0.3)
    for f in (fs, fc, fe, fh):
        f.close()


if __name__ == "__main__":
    main()
