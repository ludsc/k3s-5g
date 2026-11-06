#!/usr/bin/env python3
"""Análise e gráficos de uma execução (pasta runs/<data>_<cenario>/).

Uso: python3 analyze.py runs/<pasta>        (ou: python3 lab5g.py plot [pasta])

Gera em <pasta>/graficos/:
  overview.png        linha do tempo: vazão, perda, jitter, retransmissões, RTT e UPFs por pod
  tcp_<teste>.png     vazão (por fluxo), retransmissões/s, RTT e cwnd de cada teste TCP
  tcp_compare.png     compara os testes TCP (ex.: sozinho x com agressor UDP)
  udp_por_taxa.png    perda, jitter, vazão recebida x taxa oferecida (UDP)
  summary_udp.csv / summary_tcp.csv
"""
import argparse, csv, json, math, re, sys
from pathlib import Path
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

NAN = float("nan")
MULT = {"": 1, "K": 1e3, "M": 1e6, "G": 1e9}
LINE = re.compile(r"^\[\s*\d+\]\s+([\d.]+)-([\d.]+)\s+sec\s+([\d.]+)\s+\S*Bytes\s+([\d.]+)\s+(\S*)bits/sec"
                  r"\s+([\d.]+)\s+ms\s+(\d+)/(\d+)\s+\(([\d.eE+-]+)%\)")


# ------------------------------------------------------------------ utilidades
def read_csv(p):
    p = Path(p)
    if not p.exists():
        return []
    with open(p, newline="") as f:
        return list(csv.DictReader(f))


def load_json(p):
    try:
        return json.load(open(p))
    except Exception:
        return None


def mean(v):
    return sum(v) / len(v) if v else 0.0


def pctl(v, p):
    if not v:
        return 0.0
    v = sorted(v)
    k = (len(v) - 1) * p / 100.0
    f, c = math.floor(k), math.ceil(k)
    return v[f] if f == c else v[f] + (v[c] - v[f]) * (k - f)


def fnum(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ UDP
def rows_from_text(text):
    """Linhas de intervalo do receptor (log/saída de texto do servidor iperf3)."""
    rows, prev = [], -1.0
    for line in text.splitlines():
        if "receiver" in line or "sender" in line:
            continue
        m = LINE.match(line.strip())
        if not m:
            continue
        t0, t1 = float(m[1]), float(m[2])
        if t1 - t0 > 1.5:
            continue
        if t0 < prev:            # outro teste no mesmo log: fica só o último
            rows = []
        prev = t0
        rows.append(dict(t=t0, mbps=float(m[4]) * MULT.get(m[5], 1) / 1e6, jitter=float(m[6]),
                         lost=int(m[7]), pkts=int(m[8]), loss=float(m[9])))
    return rows


OOO = re.compile(r"(\d+)\s+datagrams received out-of-order")


def ooo_from_text(text):
    """Pacotes fora de ordem contados pelo receptor (servidor iperf3 em modo texto).

    O iperf3 só imprime a linha "N datagrams received out-of-order" quando N > 0, logo
    depois do resumo final ("... receiver"). Então: resumo presente sem essa linha = 0;
    sem resumo (teste interrompido, canal de controle caiu) = desconhecido (None).
    Obs.: o iperf3 desconta da perda cada pacote que chega fora de ordem.
    """
    if not text:
        return None
    pos = text.rfind("receiver")
    if pos < 0:
        return None
    ini = text.rfind("- - -", 0, pos)                # início do bloco de resumo
    fim = text.find("Server listening", pos)         # no log do servidor, o próximo teste começa aqui
    resumo = text[ini if ini >= 0 else pos: fim if fim >= 0 else len(text)]
    return sum(int(n) for n in OOO.findall(resumo))


def rows_from_json(srv):
    rows = []
    for iv in srv["intervals"]:
        s = iv["sum"]
        rows.append(dict(t=s["start"], mbps=s["bits_per_second"] / 1e6, jitter=s.get("jitter_ms", 0),
                         lost=s.get("lost_packets", 0), pkts=s.get("packets", 0), loss=s.get("lost_percent", 0)))
    end = srv.get("end", {})
    ooo = end.get("sum", {}).get("out_of_order")
    if ooo is None:
        vals = [s["udp"].get("out_of_order") for s in end.get("streams", []) if "udp" in s]
        ooo = sum(v for v in vals if v is not None) if any(v is not None for v in vals) else None
    return rows, ooo


def udp_phase(ph, rd, payload):
    d = load_json(rd / ph["json"]) or {}
    dur = int(ph["dur_s"])
    sent_pkts = sent_mbps = None
    ivs = d.get("intervals") or []
    if ivs:
        sent_pkts = sum(i["sum"].get("packets", 0) for i in ivs)
        sent_mbps = mean([i["sum"]["bits_per_second"] / 1e6 for i in ivs])
    rows, ooo, source = None, None, ""
    srv = d.get("server_output_json")
    if srv and srv.get("intervals"):
        rows, ooo = rows_from_json(srv)
        source = "iperf3 (JSON do servidor)"
    elif isinstance(d.get("server_output_text"), str):
        rows, source = rows_from_text(d["server_output_text"]), "iperf3 (texto do servidor)"
        ooo = ooo_from_text(d["server_output_text"])
    if not rows:
        log = rd / ph["server_log"]
        txt = log.read_text(errors="ignore") if log.exists() else ""
        rows = rows_from_text(txt)
        ooo = ooo_from_text(txt)
        source = "log do servidor (canal de controle caiu)" if rows else "sem dados do receptor"
    rows = [r for r in rows if r["t"] < dur]      # o servidor imprime zeros depois do fim do envio
    by_t = {int(r["t"]): r for r in rows}
    active = [r for r in rows if r["pkts"] > 0]
    total = sum(r["pkts"] for r in rows)
    lost = sum(r["lost"] for r in rows)
    zero = sum(1 for s in range(dur) if by_t.get(s, {"pkts": 0})["pkts"] == 0) if rows else dur
    s = dict(
        fase=ph["name"], papel=ph["role"], oferecido_mbps=fnum(ph["rate_mbps"]),
        enviado_mbps=round(sent_mbps, 2) if sent_mbps is not None else "",
        recebido_mbps=round(sum(r["mbps"] for r in rows) / dur, 2) if rows else 0,
        perda_pct=round(100.0 * lost / total, 3) if total else 0.0,
        pacotes_perdidos=lost, bytes_perdidos_est=lost * payload,
        pacotes_enviados=sent_pkts if sent_pkts is not None else "",
        pacotes_recebidos=(total - lost) if rows else "",
        jitter_medio_ms=round(mean([r["jitter"] for r in active]), 3),
        jitter_p95_ms=round(pctl([r["jitter"] for r in active], 95), 3),
        jitter_max_ms=round(max([r["jitter"] for r in active], default=0), 3),
        seg_sem_trafego=zero, fora_de_ordem=ooo if ooo is not None else "n/d",
        fora_de_ordem_pct=round(100.0 * ooo / (total - lost), 4) if ooo is not None and total - lost > 0 else "n/d",
        teste_ok=ph["ok"], fonte=source)
    return dict(p=ph, rows=rows, s=s)


# ------------------------------------------------------------------ TCP
def tcp_phase(ph, rd):
    d = load_json(rd / ph["json"]) or {}
    ivs = d.get("intervals") or []
    t, agg, retr, rtt_m, rtt_x = [], [], [], [], []
    streams = {}
    for iv in ivs:
        t.append(iv["sum"]["start"])
        agg.append(iv["sum"]["bits_per_second"] / 1e6)
        retr.append(iv["sum"].get("retransmits", 0) or 0)
        rt = [s["rtt"] / 1000.0 for s in iv["streams"] if s.get("rtt")]
        rtt_m.append(mean(rt)); rtt_x.append(max(rt, default=0))
        for k, s in enumerate(iv["streams"]):
            st = streams.setdefault(k, dict(mbps=[], cwnd=[]))
            st["mbps"].append(s["bits_per_second"] / 1e6)
            st["cwnd"].append((s.get("snd_cwnd") or 0) / 1024.0)
    drops = 0
    for st in streams.values():
        c = st["cwnd"]
        drops += sum(1 for i in range(1, len(c)) if c[i - 1] > 0 and c[i] < 0.5 * c[i - 1])
    mm = mean(agg)
    zero = sum(1 for v in agg if v < 0.01 * mm) if mm > 0 else len(agg)
    end_recv = (d.get("end") or {}).get("sum_received", {}).get("bits_per_second")
    s = dict(fase=ph["name"], papel=ph["role"], fluxos=len(streams),
             vazao_media_mbps=round(end_recv / 1e6 if end_recv else mm, 2),
             vazao_min_mbps=round(min(agg, default=0), 2),
             retransmissoes_total=int(sum(retr)), retrans_por_s_media=round(mean(retr), 2),
             retrans_por_s_max=max(retr, default=0),
             rtt_medio_ms=round(mean(rtt_m), 2), rtt_p95_ms=round(pctl(rtt_x, 95), 2),
             rtt_max_ms=round(max(rtt_x, default=0), 2),
             cwnd_min_kb=round(min([min(st["cwnd"]) for st in streams.values()], default=0), 1),
             quedas_de_cwnd=drops, seg_sem_trafego=zero, teste_ok=ph["ok"])
    return dict(p=ph, t=t, agg=agg, retr=retr, rtt=rtt_m, rtt_max=rtt_x, streams=streams, s=s)


# ------------------------------------------------------------------ UPF
def load_upf(rd):
    pods = {}
    for r in read_csv(rd / "upf_samples.csv"):
        try:
            pods.setdefault(r["pod"], []).append(
                (float(r["ts"]), *(int(r[k]) for k in ("rx_bytes", "tx_bytes", "rx_pkts", "tx_pkts",
                                                          "rx_drop", "tx_drop", "cpu_usec"))))
        except (ValueError, KeyError):
            pass
    return pods


def upf_series(pods, t0):
    out = {}
    for pod, rows in pods.items():
        x, rx, tx, cpu = [], [], [], []
        for a, b in zip(rows, rows[1:]):
            dt = b[0] - a[0]
            if dt <= 0.2:
                continue
            x.append(b[0] - t0)
            rx.append(max(b[1] - a[1], 0) * 8 / dt / 1e6)
            tx.append(max(b[2] - a[2], 0) * 8 / dt / 1e6)
            cpu.append(max(b[7] - a[7], 0) / dt / 1e6)
        out[pod] = (x, rx, tx, cpu)
    return out


def upf_delta(pods, a, b):
    d = dict(upf_rx_pkts=0, upf_tx_pkts=0, upf_rx_drop=0, upf_cpu_cores=0.0)
    if b <= a:
        return d
    for rows in pods.values():
        def at(t):
            best = rows[0]
            for r in rows:
                if r[0] <= t:
                    best = r
            return best
        r0, r1 = at(a), at(b)
        d["upf_rx_pkts"] += r1[3] - r0[3]
        d["upf_tx_pkts"] += r1[4] - r0[4]
        d["upf_rx_drop"] += r1[5] - r0[5]
        if r1[0] > r0[0]:
            d["upf_cpu_cores"] += (r1[7] - r0[7]) / (r1[0] - r0[0]) / 1e6
    d["upf_cpu_cores"] = round(d["upf_cpu_cores"], 2)
    return d


# ------------------------------------------------------------------ gráficos
def concat(parts):
    xs, ys = [], []
    for x, y in parts:
        if xs:
            xs.append(xs[-1] + 0.01); ys.append(NAN)
        xs += x; ys += y
    return xs, ys


def overview(gd, t0, udp, tcp, pods, events, phases):
    P = {k: [] for k in ("tput", "loss", "jitter", "retr", "rtt", "upf_tp", "upf_cpu")}
    grp = {}
    for u in udp:
        ph, rows = u["p"], u["rows"]
        x0 = ph["t_start"] - t0
        if ph["rate_mbps"]:
            P["tput"].append(("oferecido (UDP)", [x0, x0 + ph["dur_s"]], [ph["rate_mbps"]] * 2,
                              dict(color="gray", ls="--", lw=1.2)))
        if rows:
            g = grp.setdefault(ph["role"], dict(t=[], l=[], j=[]))
            xs = [x0 + r["t"] for r in rows]
            g["t"].append((xs, [r["mbps"] for r in rows]))
            g["l"].append((xs, [r["loss"] for r in rows]))
            g["j"].append((xs, [r["jitter"] for r in rows]))
    for role, g in grp.items():
        P["tput"].append((f"UDP {role} (recebido)",) + concat(g["t"]) + ({},))
        P["loss"].append((f"UDP {role}",) + concat(g["l"]) + ({},))
        P["jitter"].append((f"UDP {role}",) + concat(g["j"]) + ({},))
    for tp in tcp:
        ph = tp["p"]
        xs = [ph["t_start"] - t0 + v for v in tp["t"]]
        if xs:
            P["tput"].append((f"TCP {ph['name']}", xs, tp["agg"], {}))
            P["retr"].append((ph["name"], xs, tp["retr"], {}))
            P["rtt"].append((ph["name"], xs, tp["rtt"], {}))
    for pod, (x, rx, tx, cpu) in upf_series(pods, t0).items():
        short = pod if len(pod) <= 26 else pod[-26:]
        P["upf_tp"].append((f"{short} UL(rx)", x, rx, {}))
        P["upf_tp"].append((f"{short} DL(tx)", x, tx, dict(ls=":")))
        P["upf_cpu"].append((short, x, cpu, {}))
    titles = dict(tput="Vazão (Mbps)", loss="Perda UDP (%)", jitter="Jitter UDP (ms)",
                  retr="Retransmissões TCP (/s)", rtt="RTT TCP médio (ms)",
                  upf_tp="UPF por pod: vazão em ogstun (Mbps)", upf_cpu="UPF por pod: CPU (cores)")
    keys = [k for k in P if P[k]]
    if not keys:
        return
    fig, axes = plt.subplots(len(keys), 1, figsize=(12, 2.5 * len(keys) + 1.2), sharex=True, squeeze=False)
    for ax, k in zip(axes[:, 0], keys):
        for lab, x, y, kw in P[k]:
            ax.plot(x, y, label=lab, **kw)
        ax.set_ylabel(titles[k], fontsize=8); ax.grid(alpha=0.3); ax.legend(fontsize=7, loc="upper left", ncol=2)
    top = axes[0, 0]
    for ph in phases:
        x = ph["t_start"] - t0
        for ax in axes[:, 0]:
            ax.axvline(x, color="k", ls=":", alpha=0.25)
        top.text(x, 0.98, ph["name"], transform=top.get_xaxis_transform(), rotation=90, va="top", fontsize=6)
    seen = set()
    for e in events:
        if e["event"] in ("pod_added", "pod_removed"):
            col = "green" if e["event"] == "pod_added" else "red"
            lab = "UPF criada" if e["event"] == "pod_added" else "UPF removida"
            for ax in axes[:, 0]:
                ax.axvline(float(e["ts"]) - t0, color=col, lw=1.8, alpha=0.8, label=lab if lab not in seen else None)
            seen.add(lab)
    if seen:
        axes[-1, 0].legend(fontsize=7, loc="upper left", ncol=3)
    axes[-1, 0].set_xlabel("Tempo desde o início (s)")
    fig.tight_layout(); fig.savefig(gd / "overview.png", dpi=140); plt.close(fig)


def tcp_plot(gd, tp):
    ph = tp["p"]
    fig, ax = plt.subplots(4, 1, figsize=(10, 10), sharex=True)
    for k, st in tp["streams"].items():
        ax[0].plot(tp["t"], st["mbps"], lw=0.9, alpha=0.7, label=f"fluxo {k}")
        ax[3].plot(tp["t"], st["cwnd"], lw=0.9, alpha=0.7, label=f"fluxo {k}")
    ax[0].plot(tp["t"], tp["agg"], "k", lw=1.8, label="soma")
    ax[1].bar(tp["t"], tp["retr"], width=0.8)
    ax[2].plot(tp["t"], tp["rtt"], label="médio"); ax[2].plot(tp["t"], tp["rtt_max"], label="máx. entre fluxos", alpha=0.6)
    for a, lab in zip(ax, ("Vazão (Mbps)", "Retransmissões (/s)", "RTT (ms)", "cwnd (KB)")):
        a.set_ylabel(lab); a.grid(alpha=0.3)
    ax[0].legend(fontsize=7, ncol=3); ax[2].legend(fontsize=7); ax[3].legend(fontsize=7, ncol=3)
    ax[3].set_xlabel("Tempo (s)")
    fig.suptitle(f"TCP – {ph['name']}"); fig.tight_layout()
    fig.savefig(gd / f"tcp_{ph['name']}.png", dpi=140); plt.close(fig)


def tcp_compare(gd, tcps):
    fig, ax = plt.subplots(4, 1, figsize=(10, 10), sharex=True)
    for tp in tcps:
        n = tp["p"]["name"]
        c = [sum(v) for v in zip(*[st["cwnd"] for st in tp["streams"].values()])] if tp["streams"] else []
        ax[0].plot(tp["t"], tp["agg"], label=n)
        ax[1].plot(tp["t"], tp["retr"], label=n)
        ax[2].plot(tp["t"], tp["rtt"], label=n)
        ax[3].plot(tp["t"][:len(c)], c, label=n)
    for a, lab in zip(ax, ("Vazão total (Mbps)", "Retransmissões (/s)", "RTT médio (ms)", "cwnd somado (KB)")):
        a.set_ylabel(lab); a.grid(alpha=0.3); a.legend(fontsize=8)
    ax[3].set_xlabel("Tempo desde o início de cada teste (s)")
    fig.tight_layout(); fig.savefig(gd / "tcp_compare.png", dpi=140); plt.close(fig)


def udp_by_rate(gd, summ):
    xs = sorted([s for s in summ if s["oferecido_mbps"] and s["papel"] == "main"], key=lambda s: s["oferecido_mbps"])
    if len(xs) < 2:
        return
    x = [s["oferecido_mbps"] for s in xs]
    fig, ax = plt.subplots(2, 2, figsize=(10, 7)); ax = ax.ravel()
    for a, (k, lab) in zip(ax, (("recebido_mbps", "Vazão recebida (Mbps)"), ("perda_pct", "Perda (%)"),
                                ("jitter_medio_ms", "Jitter médio (ms)"), ("seg_sem_trafego", "Segundos sem tráfego"))):
        a.plot(x, [s[k] for s in xs], marker="o")
        if k == "recebido_mbps":
            a.plot(x, x, "k:", lw=1, label="ideal"); a.legend(fontsize=8)
        a.set_xlabel("Taxa oferecida (Mbps)"); a.set_ylabel(lab); a.grid(alpha=0.3)
    fig.tight_layout(); fig.savefig(gd / "udp_por_taxa.png", dpi=140); plt.close(fig)


def write_csv(path, rows):
    if not rows:
        return
    keys = list(rows[0].keys())
    for r in rows:
        for k in r:
            if k not in keys:
                keys.append(k)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader(); w.writerows(rows)


def table(rows, cols):
    if not rows:
        return
    w = [max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in cols]
    print("  ".join(c.ljust(n) for c, n in zip(cols, w)))
    for r in rows:
        print("  ".join(str(r.get(c, "")).ljust(n) for c, n in zip(cols, w)))


def analyze(rd):
    rd = Path(rd)
    meta = load_json(rd / "meta.json") or {}
    payload = int(meta.get("payload", 1200))
    phases = read_csv(rd / "phases.csv")
    if not phases:
        print("!! phases.csv vazio: nenhum teste foi registrado."); return
    for p in phases:
        for k in ("rate_mbps", "t_start", "t_end", "dur_s"):
            p[k] = fnum(p.get(k))
    t0 = min(p["t_start"] for p in phases)
    pods, events = load_upf(rd), read_csv(rd / "events.csv")
    gd = rd / "graficos"; gd.mkdir(exist_ok=True)

    udp = [udp_phase(p, rd, payload) for p in phases if p["kind"] == "udp"]
    tcp = [tcp_phase(p, rd) for p in phases if p["kind"] == "tcp"]
    for u in udp:                       # contadores da UPF em cada fase UDP
        p = u["p"]
        u["s"].update(upf_delta(pods, p["t_start"] + 0.5, p["t_end"])) if pods else None
    for t in tcp:
        p = t["p"]
        t["s"].update(upf_delta(pods, p["t_start"], p["t_end"])) if pods else None

    overview(gd, t0, udp, tcp, pods, events, phases)
    for t in tcp:
        if t["t"]:
            tcp_plot(gd, t)
    mains = [t for t in tcp if t["p"]["role"] == "main" and t["t"]]
    if len(mains) >= 2:
        tcp_compare(gd, mains)
    su, st = [u["s"] for u in udp], [t["s"] for t in tcp]
    udp_by_rate(gd, su)
    write_csv(gd / "summary_udp.csv", su); write_csv(gd / "summary_tcp.csv", st)

    if su:
        print("\n=== UDP (receptor) ===")
        table(su, ["fase", "papel", "oferecido_mbps", "recebido_mbps", "perda_pct", "pacotes_perdidos",
                   "jitter_medio_ms", "jitter_p95_ms", "seg_sem_trafego", "fora_de_ordem", "teste_ok"])
        print("\nContadores da UPF (soma dos pods) e fonte dos dados:")
        table(su, ["fase", "pacotes_enviados", "upf_rx_pkts", "pacotes_recebidos", "upf_rx_drop", "upf_cpu_cores", "fonte"])
    if st:
        print("\n=== TCP (emissor) ===")
        table(st, ["fase", "papel", "fluxos", "vazao_media_mbps", "vazao_min_mbps", "retransmissoes_total",
                   "retrans_por_s_max", "rtt_medio_ms", "rtt_max_ms", "cwnd_min_kb", "quedas_de_cwnd",
                   "seg_sem_trafego", "teste_ok"])
    if events:
        print("\n=== Eventos de UPF ===")
        for e in events:
            print(f"  t={float(e['ts']) - t0:7.1f}s  {e['event']:12} {e['detail']}")
    print(f"\n>> Gráficos e CSVs em {gd}/")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("rundir")
    analyze(ap.parse_args().rundir)
