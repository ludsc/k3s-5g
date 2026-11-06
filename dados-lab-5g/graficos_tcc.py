#!/usr/bin/env python3
"""Consolida as repetições dos experimentos e gera os gráficos do relatório.

Uso (na pasta dados-lab-5g, depois de rodar cap / ladder / tcpsat várias vezes):

  python3 graficos_tcc.py                     # lê runs/, grava em consolidado/
  python3 graficos_tcc.py --runs runs --out consolidado --formato pdf
  python3 graficos_tcc.py --desde 20261105    # só execuções a partir dessa data

Cada pasta runs/<data>_<cenario>/ conta como uma repetição do seu cenário.
Os números são recalculados a partir dos dados brutos (raw/*.json, logs do
servidor e upf_samples.csv) com as mesmas funções do analyze.py, então não
depende de os gráficos individuais terem sido gerados.

Saídas em <out>/:
  fig01_capacidade          vazão TCP com 1 e 4 fluxos (define C)
  fig02_udp_perda           perda UDP x carga (% de C), com pacotes/s no eixo de cima
  fig03_udp_vazao           vazão recebida x oferecida, com a linha ideal
  fig04_udp_jitter          jitter médio x carga
  fig05_upf_cpu             CPU da UPF x carga
  fig06_local_da_perda      perda antes da UPF (N3/UERANSIM) x depois da UPF (N6)
  fig07_tcp_metricas        TCP sozinho x com agressor: vazão, RTT, retransmissão %, quedas de cwnd
  fig08_tcp_serie           vazão e RTT ao longo do teste (média das repetições, faixa = mín–máx)
  fig09_udp_fora_de_ordem   % de pacotes fora de ordem x carga (só se algum foi observado)
  tabela_cap.csv / tabela_udp.csv / tabela_tcp.csv     média, desvio, IC 95% e n
  repeticoes_cap.csv / repeticoes_udp.csv / repeticoes_tcp.csv   um valor por repetição

Barras de erro = intervalo de confiança de 95% (t de Student, n-1 graus de liberdade).
Com n = 1 não há intervalo; o script avisa.
"""
import argparse, csv, json, math, re, sys
from collections import defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import analyze as an  # noqa: E402  (mesmas regras de cálculo dos gráficos individuais)

# Paleta validada (categórica, 2 séries) e tinta neutra para texto/eixos
AZUL, LARANJA = "#2a78d6", "#eb6834"
TINTA, TINTA2, GRADE = "#1f1f1e", "#5f5e58", "#e6e5e0"
COND = {"tcp_sozinho": ("TCP sozinho", AZUL), "tcp_com_agressor": ("TCP com agressor UDP", LARANJA)}

plt.rcParams.update({
    "figure.facecolor": "white", "axes.facecolor": "white", "savefig.facecolor": "white",
    "axes.edgecolor": TINTA2, "axes.labelcolor": TINTA, "xtick.color": TINTA2, "ytick.color": TINTA2,
    "text.color": TINTA, "axes.grid": True, "grid.color": GRADE, "grid.linewidth": 0.8,
    "axes.spines.top": False, "axes.spines.right": False, "axes.axisbelow": True,
    "font.size": 10, "axes.titlesize": 10.5, "axes.titleweight": "bold", "axes.titlelocation": "left",
    "legend.frameon": False, "lines.linewidth": 2,
})


# ------------------------------------------------------------------ estatística
def t975(gl):
    try:
        from scipy.stats import t
        return float(t.ppf(0.975, gl))
    except Exception:
        tab = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306,
               9: 2.262, 10: 2.228, 15: 2.131, 20: 2.086, 30: 2.042}
        return tab.get(gl) or tab[min(tab, key=lambda k: abs(k - gl))]


def resumo(vals):
    v = [x for x in vals if x is not None and not (isinstance(x, float) and math.isnan(x))]
    n = len(v)
    if not n:
        return dict(n=0, media=float("nan"), desvio=float("nan"), ic95=float("nan"), minimo=float("nan"), maximo=float("nan"))
    m = sum(v) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in v) / (n - 1)) if n > 1 else 0.0
    ic = t975(n - 1) * sd / math.sqrt(n) if n > 1 else 0.0
    return dict(n=n, media=m, desvio=sd, ic95=ic, minimo=min(v), maximo=max(v))


def fmt(x, nd=3):
    return "" if x is None or (isinstance(x, float) and math.isnan(x)) else round(x, nd)


# ------------------------------------------------------------------ leitura das execuções
def carregar_execucao(rd):
    """Recalcula as métricas de uma pasta de execução com as funções do analyze.py."""
    meta = an.load_json(rd / "meta.json") or {}
    payload = int(meta.get("payload", 1200))
    phases = an.read_csv(rd / "phases.csv")
    for p in phases:
        for k in ("rate_mbps", "t_start", "t_end", "dur_s"):
            p[k] = an.fnum(p.get(k))
    pods = an.load_upf(rd)
    udp = [an.udp_phase(p, rd, payload) for p in phases if p["kind"] == "udp"]
    tcp = [an.tcp_phase(p, rd) for p in phases if p["kind"] == "tcp"]
    for u in udp:
        if pods:
            u["s"].update(an.upf_delta(pods, u["p"]["t_start"] + 0.5, u["p"]["t_end"]))
            # Para CONTAR pacotes, a janela precisa cobrir a fase inteira: o amostrador lê a 1 Hz e
            # a janela estreita acima perde até ~1 s de tráfego. As pausas entre fases (3 s) não têm
            # tráfego, então alargar a janela não mistura fases (exceto se a sonda estiver ativa).
            larga = an.upf_delta(pods, u["p"]["t_start"] - 1.2, u["p"]["t_end"] + 1.5)
            u["s"]["upf_rx_pkts_fase"] = larga["upf_rx_pkts"]
    for t in tcp:
        if pods:
            t["s"].update(an.upf_delta(pods, t["p"]["t_start"], t["p"]["t_end"]))
        t["s"]["retrans_pct"] = retrans_pct(rd / t["p"]["json"], t["s"]["retransmissoes_total"])
    return meta, payload, udp, tcp


def retrans_pct(jpath, retrans):
    """Retransmissões como % dos segmentos enviados (bytes enviados / MSS)."""
    d = an.load_json(jpath) or {}
    sent = ((d.get("end") or {}).get("sum_sent") or {}).get("bytes")
    mss = (d.get("start") or {}).get("tcp_mss_default") or 1448
    if not sent:
        return None
    return 100.0 * retrans / (sent / mss)


def listar(runs, desde):
    out = defaultdict(list)
    for m in sorted(Path(runs).glob("*_*/meta.json")):
        rd = m.parent
        data, _, cen = rd.name.partition("_")
        if desde and data.replace("-", "") < desde:
            continue
        out[cen].append(rd)
    return out


# ------------------------------------------------------------------ helpers de gráfico
def salvar(fig, out, nome, fmt_):
    fig.tight_layout()
    fig.savefig(out / f"{nome}.{fmt_}", dpi=200, bbox_inches="tight")
    plt.close(fig)
    print(f"   {nome}.{fmt_}")


def ponto_ic(ax, x, st, cor, rotulo=None, ms=7):
    """Média como ponto + bigode do IC 95% (sem barra flutuante)."""
    ys = [s["media"] for s in st]
    es = [s["ic95"] for s in st]
    ax.errorbar(x, ys, yerr=es, fmt="o-", color=cor, ms=ms, capsize=4, elinewidth=1.5,
                markeredgecolor="white", markeredgewidth=1.5, label=rotulo, zorder=3)


def nota_n(ax, n, embaixo=True, txt="barras = IC 95%"):
    """Subtítulo com o n; embaixo=True o coloca no rodapé da figura (quando há eixo superior)."""
    msg = f"n = {n} repetições · {txt}"
    if embaixo:
        ax.figure.text(0.99, 0.005, msg, ha="right", va="bottom", fontsize=8, color=TINTA2)
    else:
        ax.title.set_y(1.06)
        ax.text(0.0, 1.015, msg, transform=ax.transAxes, ha="left", va="bottom", fontsize=8, color=TINTA2)


def write_csv(path, rows):
    if not rows:
        return
    keys = []
    for r in rows:
        keys += [k for k in r if k not in keys]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader(); w.writerows(rows)


def tabela_agregada(rep, chave_grupo, metricas):
    grupos = defaultdict(list)
    for r in rep:
        grupos[r[chave_grupo] if isinstance(chave_grupo, str) else tuple(r[k] for k in chave_grupo)].append(r)
    linhas = []
    for g, rs in grupos.items():
        lin = dict(zip(chave_grupo, g)) if not isinstance(chave_grupo, str) else {chave_grupo: g}
        for m in metricas:
            s = resumo([an.fnum(r.get(m)) for r in rs])
            lin.update({f"{m}_media": fmt(s["media"]), f"{m}_desvio": fmt(s["desvio"]),
                        f"{m}_ic95": fmt(s["ic95"]), f"{m}_n": s["n"]})
        linhas.append(lin)
    return linhas, grupos


# ------------------------------------------------------------------ cenário 1: capacidade
def cenario_cap(dirs, out, f):
    rep = []
    for i, rd in enumerate(dirs, 1):
        _, _, _, tcp = carregar_execucao(rd)
        for t in tcp:
            s = t["s"]
            rep.append(dict(repeticao=i, pasta=rd.name, teste=s["fase"], fluxos=s["fluxos"],
                            vazao_mbps=s["vazao_media_mbps"], retrans_pct=fmt(s["retrans_pct"]),
                            rtt_medio_ms=s["rtt_medio_ms"], teste_ok=s["teste_ok"]))
    if not rep:
        return None
    write_csv(out / "repeticoes_cap.csv", rep)
    tab, grupos = tabela_agregada(rep, "teste", ["vazao_mbps", "retrans_pct", "rtt_medio_ms"])
    write_csv(out / "tabela_cap.csv", tab)

    nomes = sorted(grupos)
    st = [resumo([r["vazao_mbps"] for r in grupos[k]]) for k in nomes]
    C = max(s["media"] for s in st)
    fig, ax = plt.subplots(figsize=(5.5, 3.6))
    for k, nm in enumerate(nomes):
        ax.scatter([k] * len(grupos[nm]), [r["vazao_mbps"] for r in grupos[nm]], s=16, color=GRADE,
                   edgecolor=TINTA2, linewidth=0.5, zorder=2)
    ax.errorbar(range(len(nomes)), [s["media"] for s in st], yerr=[s["ic95"] for s in st], fmt="o",
                color=AZUL, ms=9, capsize=5, elinewidth=1.5, markeredgecolor="white", zorder=3)
    for k, s in enumerate(st):
        ax.annotate(f"{s['media']:.1f} Mbps", (k, s["media"]), xytext=(12, 0), textcoords="offset points",
                    va="center", fontsize=9)
    ax.set_xticks(range(len(nomes)), [n.replace("tcp_P", "") + " fluxo(s)" for n in nomes])
    ax.set_xlim(-0.6, len(nomes) - 0.4)
    ax.set_ylim(0, C * 1.25)
    ax.set_ylabel("Vazão TCP (Mbps)")
    ax.set_title(f"Capacidade efetiva do caminho: C ≈ {C:.0f} Mbps")
    nota_n(ax, max(s["n"] for s in st))
    salvar(fig, out, "fig01_capacidade", f)
    return C


# ------------------------------------------------------------------ cenário 2: escada UDP
def pct_da_fase(nome):
    m = re.search(r"_([\d.]+)pct", nome)
    return float(m.group(1)) if m else None


def cenario_ladder(dirs, out, f, C_ref):
    rep = []
    payload = 1200
    for i, rd in enumerate(dirs, 1):
        meta, payload, udp, _ = carregar_execucao(rd)
        for u in udp:
            s = u["s"]
            if s["papel"] != "main" or s["fonte"] == "sem dados do receptor":
                continue
            env = an.fnum(s.get("pacotes_enviados"))
            rx_upf = an.fnum(s.get("upf_rx_pkts_fase"))
            rec = an.fnum(s.get("pacotes_recebidos"))
            antes = depois = None
            if env and rx_upf is not None and rec is not None:
                antes = max(0.0, 100.0 * (env - rx_upf) / env)       # não chegou à ogstun
                depois = max(0.0, 100.0 * (rx_upf - rec) / env)      # passou pela UPF e sumiu depois
            rep.append(dict(repeticao=i, pasta=rd.name, carga_pct=pct_da_fase(s["fase"]),
                            oferecido_mbps=s["oferecido_mbps"], recebido_mbps=s["recebido_mbps"],
                            pps_oferecido=round(s["oferecido_mbps"] * 1e6 / 8 / payload),
                            perda_pct=s["perda_pct"], pacotes_perdidos=s["pacotes_perdidos"],
                            bytes_perdidos=s["bytes_perdidos_est"], jitter_medio_ms=s["jitter_medio_ms"],
                            jitter_p95_ms=s["jitter_p95_ms"], fora_de_ordem=s["fora_de_ordem"],
                            fora_de_ordem_pct=s.get("fora_de_ordem_pct", "n/d"),
                            upf_cpu_cores=s.get("upf_cpu_cores"), perda_antes_upf_pct=fmt(antes),
                            perda_depois_upf_pct=fmt(depois), teste_ok=s["teste_ok"]))
    if not rep:
        return
    write_csv(out / "repeticoes_udp.csv", rep)
    metr = ["oferecido_mbps", "recebido_mbps", "perda_pct", "pacotes_perdidos", "bytes_perdidos",
            "jitter_medio_ms", "jitter_p95_ms", "fora_de_ordem", "fora_de_ordem_pct",
            "upf_cpu_cores", "perda_antes_upf_pct", "perda_depois_upf_pct"]
    tab, grupos = tabela_agregada(rep, "carga_pct", metr)
    tab.sort(key=lambda r: r["carga_pct"])
    write_csv(out / "tabela_udp.csv", tab)

    cargas = sorted(grupos)
    S = {m: [resumo([an.fnum(r[m]) for r in grupos[c]]) for c in cargas] for m in metr}
    n = max(s["n"] for s in S["perda_pct"])
    of_med = [s["media"] for s in S["oferecido_mbps"]]
    # C da própria escada (taxa oferecida no degrau de 100%); se não houver, o do cenário cap
    C = of_med[cargas.index(100.0)] if 100.0 in cargas else C_ref

    # fig02 — perda x carga, com pacotes/s no eixo superior (mesma escala, só outra unidade)
    fig, ax = plt.subplots(figsize=(6.5, 4))
    ponto_ic(ax, cargas, S["perda_pct"], AZUL)
    for c, s in zip(cargas, S["perda_pct"]):
        ax.annotate(f"{s['media']:.1f}%", (c, s["media"]), xytext=(0, 9), textcoords="offset points",
                    ha="center", fontsize=8.5)
    ax.axvline(100, color=TINTA2, ls=":", lw=1)
    ax.text(100, 0.98, " C", transform=ax.get_xaxis_transform(), va="top", fontsize=8.5, color=TINTA2)
    ax.set_xlabel("Carga UDP oferecida (% de C)")
    ax.set_ylabel("Perda de pacotes (%)")
    ax.set_ylim(bottom=0)
    if C:
        k = C * 1e6 / 8 / payload / 100.0    # pacotes/s por ponto percentual
        sec = ax.secondary_xaxis("top", functions=(lambda x: x * k, lambda y: y / k))
        sec.set_xlabel("Pacotes por segundo oferecidos", fontsize=8.5, color=TINTA2)
        sec.tick_params(labelsize=8)
    ax.set_title("Perda UDP cresce de forma acelerada com a carga", pad=28)
    nota_n(ax, n, embaixo=True)
    salvar(fig, out, "fig02_udp_perda", f)

    # fig03 — vazão recebida x oferecida
    fig, ax = plt.subplots(figsize=(6.5, 4))
    lim = max(of_med) * 1.08
    ax.plot([0, lim], [0, lim], ls=":", color=TINTA2, lw=1.2, label="ideal (recebido = oferecido)")
    ax.errorbar(of_med, [s["media"] for s in S["recebido_mbps"]], yerr=[s["ic95"] for s in S["recebido_mbps"]],
                fmt="o-", color=AZUL, ms=7, capsize=4, markeredgecolor="white", markeredgewidth=1.5,
                label="vazão recebida", zorder=3)
    if C:
        ax.axvline(C, color=TINTA2, ls=":", lw=1)
        ax.text(C, 0.02, f" C = {C:.0f} Mbps", transform=ax.get_xaxis_transform(), fontsize=8.5, color=TINTA2)
    ax.set_xlim(0, lim); ax.set_ylim(0, lim)
    ax.set_xlabel("Vazão UDP oferecida (Mbps)"); ax.set_ylabel("Vazão UDP recebida (Mbps)")
    ax.set_title("A vazão recebida se afasta do ideal acima de ~70 Mbps")
    ax.legend(loc="upper left", fontsize=8.5)
    nota_n(ax, n)
    salvar(fig, out, "fig03_udp_vazao", f)

    # fig04 — jitter
    fig, ax = plt.subplots(figsize=(6.5, 3.6))
    ponto_ic(ax, cargas, S["jitter_medio_ms"], AZUL)
    ax.set_ylim(0, max(s["media"] + s["ic95"] for s in S["jitter_medio_ms"]) * 1.6)
    ax.set_xlabel("Carga UDP oferecida (% de C)"); ax.set_ylabel("Jitter médio (ms)")
    ax.set_title("Jitter permanece baixo mesmo com perda alta")
    nota_n(ax, n)
    salvar(fig, out, "fig04_udp_jitter", f)

    # fig09 — pacotes fora de ordem (RFC 4737); só faz sentido desenhar se algum foi observado
    oo = S["fora_de_ordem_pct"]
    if not any(st["n"] for st in oo):
        print("   (fora de ordem: n/d em todas as fases — servidor sem resumo final)")
    elif max(st["maximo"] for st in oo if st["n"]) == 0:
        print("   (fora de ordem: 0 em todas as fases e repetições — sem gráfico; cite o zero na tabela)")
    else:
        fig, ax = plt.subplots(figsize=(6.5, 3.6))
        ponto_ic(ax, cargas, oo, AZUL)
        ax.set_ylim(bottom=0)
        ax.set_xlabel("Carga UDP oferecida (% de C)"); ax.set_ylabel("Fora de ordem (% dos recebidos)")
        ax.set_title("Pacotes recebidos fora de ordem por degrau de carga")
        nota_n(ax, n)
        salvar(fig, out, "fig09_udp_fora_de_ordem", f)

    # fig05 — CPU da UPF
    if any(s["n"] for s in S["upf_cpu_cores"]):
        fig, ax = plt.subplots(figsize=(6.5, 3.6))
        ponto_ic(ax, cargas, S["upf_cpu_cores"], AZUL)
        ax.axhline(1.0, color=TINTA2, ls="--", lw=1)
        ax.text(cargas[0], 1.0, "1 núcleo", va="bottom", fontsize=8.5, color=TINTA2)
        ax.set_ylim(0, max(1.3, max(s["media"] + s["ic95"] for s in S["upf_cpu_cores"]) * 1.15))
        ax.set_xlabel("Carga UDP oferecida (% de C)"); ax.set_ylabel("CPU da UPF (núcleos)")
        ax.set_title("Uso de CPU da UPF por degrau de carga")
        nota_n(ax, n)
        salvar(fig, out, "fig05_upf_cpu", f)

    # fig06 — onde a perda acontece (partes de um todo: barras empilhadas)
    if any(s["n"] for s in S["perda_antes_upf_pct"]):
        a = [s["media"] for s in S["perda_antes_upf_pct"]]
        d = [s["media"] for s in S["perda_depois_upf_pct"]]
        fig, ax = plt.subplots(figsize=(6.5, 4))
        x = range(len(cargas)); w = 0.6
        ax.bar(x, a, w, color=AZUL, label="antes da UPF (N3 / UERANSIM)", edgecolor="white", linewidth=2)
        ax.bar(x, d, w, bottom=a, color=LARANJA, label="depois da UPF (N6 → servidor)", edgecolor="white", linewidth=2)
        for k in x:
            ax.annotate(f"{a[k] + d[k]:.1f}%", (k, a[k] + d[k]), xytext=(0, 4), textcoords="offset points",
                        ha="center", fontsize=8.5)
        ax.set_xticks(list(x), [f"{c:g}%" for c in cargas])
        ax.set_xlabel("Carga UDP oferecida (% de C)"); ax.set_ylabel("Pacotes perdidos (% dos enviados)")
        ax.set_title("Em que trecho do caminho os pacotes se perdem")
        ax.legend(loc="upper left", fontsize=8.5)
        ax.grid(axis="x", visible=False)
        nota_n(ax, n)
        salvar(fig, out, "fig06_local_da_perda", f)


# ------------------------------------------------------------------ cenário 3: TCP sob saturação
def cenario_tcpsat(dirs, out, f):
    rep, series = [], defaultdict(list)
    for i, rd in enumerate(dirs, 1):
        _, _, udp, tcp = carregar_execucao(rd)
        agr = next((u["s"] for u in udp if u["s"]["papel"] == "aggressor"), {})
        for t in tcp:
            s = t["s"]
            if s["fase"] not in COND:
                continue
            rep.append(dict(repeticao=i, pasta=rd.name, condicao=s["fase"], vazao_mbps=s["vazao_media_mbps"],
                            vazao_min_mbps=s["vazao_min_mbps"], rtt_medio_ms=s["rtt_medio_ms"],
                            rtt_p95_ms=s["rtt_p95_ms"], rtt_max_ms=s["rtt_max_ms"],
                            retransmissoes=s["retransmissoes_total"], retrans_pct=fmt(s["retrans_pct"]),
                            quedas_cwnd=s["quedas_de_cwnd"], upf_cpu_cores=s.get("upf_cpu_cores"),
                            agressor_perda_pct=agr.get("perda_pct") if s["fase"] == "tcp_com_agressor" else "",
                            agressor_fora_de_ordem=agr.get("fora_de_ordem") if s["fase"] == "tcp_com_agressor" else "",
                            teste_ok=s["teste_ok"]))
            series[s["fase"]].append((t["t"], t["agg"], t["rtt"]))
    if not rep:
        return
    write_csv(out / "repeticoes_tcp.csv", rep)
    metr = ["vazao_mbps", "vazao_min_mbps", "rtt_medio_ms", "rtt_p95_ms", "rtt_max_ms",
            "retrans_pct", "quedas_cwnd", "upf_cpu_cores", "agressor_perda_pct", "agressor_fora_de_ordem"]
    tab, grupos = tabela_agregada(rep, "condicao", metr)
    tab.sort(key=lambda r: list(COND).index(r["condicao"]))
    write_csv(out / "tabela_tcp.csv", tab)

    conds = [c for c in COND if c in grupos]
    n = max(len(grupos[c]) for c in conds)

    # fig07 — small multiples: cada métrica no seu próprio eixo (nunca dois eixos y)
    paineis = [("vazao_mbps", "Vazão média (Mbps)"), ("rtt_medio_ms", "RTT médio (ms)"),
               ("rtt_p95_ms", "RTT p95 (ms)"), ("retrans_pct", "Retransmissão (% dos segmentos)"),
               ("quedas_cwnd", "Quedas de cwnd (por teste)")]
    fig, axes = plt.subplots(1, len(paineis), figsize=(13, 3.8))
    for ax, (m, rot) in zip(axes, paineis):
        for k, c in enumerate(conds):
            vals = [an.fnum(r[m]) for r in grupos[c]]
            s = resumo(vals)
            nome, cor = COND[c]
            ax.bar(k, s["media"], 0.62, color=cor, edgecolor="white", linewidth=2, label=nome)
            if s["n"] > 1:
                ax.errorbar(k, s["media"], yerr=s["ic95"], color=TINTA, capsize=4, elinewidth=1.3)
            ax.scatter([k] * len(vals), vals, s=12, color="white", edgecolor=TINTA, linewidth=0.7, zorder=3)
            ax.annotate(f"{s['media']:.1f}", (k, s["media"] + (s["ic95"] or 0)), xytext=(0, 4),
                        textcoords="offset points", ha="center", fontsize=8.5)
        ax.set_title(rot, fontsize=9.5)
        ax.set_xticks([]); ax.grid(axis="x", visible=False)
        ax.set_ylim(0, ax.get_ylim()[1] * 1.12)
    handles = [plt.Rectangle((0, 0), 1, 1, color=COND[c][1]) for c in conds]
    fig.legend(handles, [COND[c][0] for c in conds], loc="upper center", ncol=2, fontsize=9.5,
               bbox_to_anchor=(0.5, 1.06))
    fig.text(0.995, -0.02, f"n = {n} repetições · barras pretas = IC 95% · pontos = repetições",
             ha="right", fontsize=8, color=TINTA2)
    salvar(fig, out, "fig07_tcp_metricas", f)

    # fig08 — série temporal média das repetições, faixa = mín–máx entre repetições
    fig, axes = plt.subplots(2, 1, figsize=(9, 5.6), sharex=True)
    for c in conds:
        nome, cor = COND[c]
        L = min(len(s[0]) for s in series[c])
        if not L:
            continue
        t = series[c][0][0][:L]
        for ax, idx in ((axes[0], 1), (axes[1], 2)):
            mat = [s[idx][:L] for s in series[c]]
            med = [sum(col) / len(col) for col in zip(*mat)]
            if len(mat) > 1:
                ax.fill_between(t, [min(col) for col in zip(*mat)], [max(col) for col in zip(*mat)],
                                color=cor, alpha=0.15, linewidth=0)
            ax.plot(t, med, color=cor, label=nome)
    axes[0].set_ylabel("Vazão TCP (Mbps)"); axes[0].set_ylim(bottom=0)
    axes[1].set_ylabel("RTT médio (ms)"); axes[1].set_ylim(bottom=0)
    axes[1].set_xlabel("Tempo desde o início do teste (s)")
    axes[0].legend(loc="center right", fontsize=8.5)
    axes[0].set_title("TCP ao longo do teste: sozinho x com agressor UDP")
    nota_n(axes[0], n, txt="linha = média · faixa = mínimo–máximo")
    salvar(fig, out, "fig08_tcp_serie", f)


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", default=str(HERE / "runs"))
    ap.add_argument("--out", default=str(HERE / "consolidado"))
    ap.add_argument("--formato", default="png", choices=["png", "pdf", "svg"])
    ap.add_argument("--desde", default="", help="AAAAMMDD: ignora execuções anteriores")
    a = ap.parse_args()

    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    grupos = listar(a.runs, a.desde)
    if not grupos:
        sys.exit(f"!! nenhuma execução encontrada em {a.runs}")
    for cen, dirs in grupos.items():
        aviso = "  (só 1: sem intervalo de confiança)" if len(dirs) == 1 else ""
        print(f">> {cen}: {len(dirs)} repetição(ões){aviso}")

    C = None
    if "cap" in grupos:
        print(">> capacidade"); C = cenario_cap(grupos["cap"], out, a.formato)
    if C is None:
        cj = Path(a.runs) / "capacity.json"
        C = json.load(open(cj))["capacity_mbps"] if cj.exists() else None
    if "ladder" in grupos:
        print(">> escada UDP"); cenario_ladder(grupos["ladder"], out, a.formato, C)
    if "tcpsat" in grupos:
        print(">> TCP sob saturação"); cenario_tcpsat(grupos["tcpsat"], out, a.formato)
    print(f"\n>> Pronto: {out}/")


if __name__ == "__main__":
    main()
