#!/usr/bin/env python3
"""Plota métricas UDP (throughput, jitter, perda, fora de ordem) de JSONs do iperf3.
Requer JSON gerado com --get-server-output (os dados de jitter/perda vêm do receptor).

Uso: python3 plot_udp.py arq1.json [arq2.json ...] --out pasta_saida
"""
import argparse, csv, json, re, sys
from pathlib import Path
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


def load_server(path):
    d = json.load(open(path))
    if d.get("error"):
        print(f"!! {path}: iperf3 reportou erro: {d['error']}")
        return None
    srv = d.get("server_output_json")
    if not srv:
        print(f"!! {path}: sem server_output_json (faltou --get-server-output?)")
        return None
    return srv


def per_run(path, outdir):
    srv = load_server(path)
    if srv is None:
        return None
    stem = Path(path).stem
    rows = []
    for iv in srv["intervals"]:
        s = iv["sum"]
        rows.append({
            "t_s": s["start"],
            "throughput_mbps": s["bits_per_second"] / 1e6,
            "jitter_ms": s.get("jitter_ms", 0),
            "loss_pct": s.get("lost_percent", 0),
            "lost_pkts": s.get("lost_packets", 0),
            "pkts": s.get("packets", 0),
        })
    with open(outdir / f"{stem}.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    end = srv["end"]
    esum = end.get("sum", {})
    ooo = esum.get("out_of_order")
    if ooo is None:
        ooo = sum(s["udp"].get("out_of_order", 0)
                  for s in end.get("streams", []) if "udp" in s)

    t = [r["t_s"] for r in rows]
    fig, ax = plt.subplots(3, 1, figsize=(9, 8), sharex=True)
    ax[0].plot(t, [r["throughput_mbps"] for r in rows]); ax[0].set_ylabel("Throughput (Mbps)")
    ax[1].plot(t, [r["jitter_ms"] for r in rows]);       ax[1].set_ylabel("Jitter (ms)")
    ax[2].plot(t, [r["loss_pct"] for r in rows]);        ax[2].set_ylabel("Perda de pacotes (%)")
    ax[2].set_xlabel("Tempo (s)")
    for a in ax:
        a.grid(alpha=0.3)
    fig.suptitle(f"{stem} | fora de ordem (total): {ooo}")
    fig.tight_layout()
    fig.savefig(outdir / f"{stem}.png", dpi=150)
    plt.close(fig)

    m = re.search(r"_(\d+)M", stem)
    return {
        "run": stem,
        "rate_mbps": int(m.group(1)) if m else None,
        "recv_mbps": esum.get("bits_per_second", 0) / 1e6,
        "jitter_ms": esum.get("jitter_ms", 0),
        "loss_pct": esum.get("lost_percent", 0),
        "lost_pkts": esum.get("lost_packets", 0),
        "pkts": esum.get("packets", 0),
        "out_of_order": ooo,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--out", default="results/udp")
    args = ap.parse_args()

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)

    summary = [s for s in (per_run(f, outdir) for f in args.files) if s]
    if not summary:
        sys.exit("nenhum arquivo válido")

    summary.sort(key=lambda s: (s["rate_mbps"] is None, s["rate_mbps"] or 0))
    with open(outdir / "summary.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(summary[0].keys()))
        w.writeheader()
        w.writerows(summary)

    print(f"{'run':28} {'rate':>6} {'recv':>9} {'jitter':>8} {'perda%':>7} {'ooo':>6}")
    for s in summary:
        print(f"{s['run']:28} {str(s['rate_mbps']):>6} {s['recv_mbps']:9.1f} "
              f"{s['jitter_ms']:8.3f} {s['loss_pct']:7.2f} {s['out_of_order']:6}")

    if len(summary) > 1:
        x = [s["rate_mbps"] if s["rate_mbps"] is not None else i
             for i, s in enumerate(summary)]
        fig, ax = plt.subplots(2, 2, figsize=(10, 7))
        ax = ax.ravel()
        series = [("recv_mbps", "Throughput recebido (Mbps)"),
                  ("jitter_ms", "Jitter (ms)"),
                  ("loss_pct", "Perda de pacotes (%)"),
                  ("out_of_order", "Pacotes fora de ordem")]
        for a, (k, lab) in zip(ax, series):
            a.plot(x, [s[k] for s in summary], marker="o")
            a.set_xlabel("Taxa oferecida (Mbps)"); a.set_ylabel(lab); a.grid(alpha=0.3)
        fig.tight_layout()
        fig.savefig(outdir / "summary_by_rate.png", dpi=150)
        plt.close(fig)

    print(f"\nSaída em {outdir}/")


if __name__ == "__main__":
    main()
