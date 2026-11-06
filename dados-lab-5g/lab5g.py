#!/usr/bin/env python3
"""Orquestrador dos experimentos (roda no HOST, com kubectl apontando para o k3s).

  python3 lab5g.py preflight        valida o ambiente (rode primeiro)
  python3 lab5g.py cap              1) mede a capacidade C do caminho (TCP)
  python3 lab5g.py ladder           2) escada UDP em % de C (padrão 25..105%) -> perda, jitter, fora de ordem
  python3 lab5g.py tcpsat           3) TCP sozinho x TCP com agressor UDP (75% de C; o TCP guloso completa o resto)
  python3 lab5g.py scale            4) carga longa (50% -> 100% de C) para observar HPA/UPFs

Teto de carga: acima de ~110% de C o emulador de RAN (UERANSIM) satura e o UE perde o sinal. Por isso o
script recusa fases acima de --max-pct (padrão 110); use --allow-high só se souber o que está fazendo.
  python3 lab5g.py plot [pasta]     refaz os gráficos de uma execução

Configuração: variáveis de ambiente ou arquivo lab.env (veja lab.env.example).
Cada execução cria runs/<data>_<cenario>/ com dados brutos, amostras da UPF e gráficos.
"""
import argparse, csv, json, os, re, subprocess, sys, time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from upf_sampler import CPU_SH   # trecho de shell que lê CPU/throttling do cgroup
PORT_MAIN, PORT_AGG, PORT_PROBE = 5301, 5302, 5303
PHASE_FIELDS = ["name", "kind", "role", "rate_mbps", "parallel", "dur_s", "t_start", "t_end",
                "rc", "ok", "json", "server_log", "valida", "motivo"]


def env(k, d=""):
    return os.environ.get(k, d)


def load_env(path):
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def die(msg):
    print("!! " + msg, file=sys.stderr)
    sys.exit(1)


def run(cmd, timeout=120):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", "timeout")


def json_error(path):
    try:
        d = json.load(open(path))
    except Exception:
        return "JSON inválido ou vazio"
    return d.get("error") or ""


class Ctx:
    def __init__(self, a):
        self.a = a
        self.rundir = None
        self.sampler = None
        self.ue_ip = None
        self.probe_ip = None
        self.has_timeout = False

    # ---- comandos kubectl ----
    def ue(self, cmd):
        a = self.a
        c = ["kubectl", "-n", a.ns, "exec", a.ue_pod]
        if a.ue_container:
            c += ["-c", a.ue_container]
        return c + ["--"] + cmd

    def srv_sh(self, script):
        a = self.a
        return run(["kubectl", "-n", a.srv_ns, "exec", a.srv_pod, "--", "sh", "-c", script])

    # ---- detecção de ambiente ----
    def ip_of(self, ifname):
        r = run(self.ue(["ping", "-c", "2", "-W", "3", "-I", ifname, self.a.dn_ip]))
        m = re.search(r"^PING .* from ([0-9.]+) ", r.stdout, re.M)
        return (m.group(1) if (r.returncode == 0 and m) else None), r.stdout + r.stderr

    def detect(self):
        a = self.a
        if not a.dn_ip:
            r = run(["kubectl", "-n", a.srv_ns, "get", "pod", a.srv_pod, "-o", "jsonpath={.status.podIP}"])
            a.dn_ip = r.stdout.strip()
            if not a.dn_ip:
                die(f"não achei o IP do pod {a.srv_pod} (ns {a.srv_ns}): {r.stderr.strip()}")
        self.ue_ip = a.ue_ip
        if not self.ue_ip:
            self.ue_ip, out = self.ip_of(a.ue_if)
            if not self.ue_ip:
                die(f"ping pelo túnel {a.ue_if} -> {a.dn_ip} falhou:\n{out}")
        if a.probe_if:
            self.probe_ip, out = self.ip_of(a.probe_if)
            if not self.probe_ip:
                print(f"!! aviso: sonda desativada, ping por {a.probe_if} falhou")
        self.has_timeout = run(self.ue(["sh", "-c", "command -v timeout"])).returncode == 0
        print(f">> DN (iperf-server): {a.dn_ip} | UE: {self.ue_ip} | timeout no pod: {self.has_timeout}")

    # ---- servidores iperf3 (portas próprias, com log em arquivo) ----
    def ensure_servers(self):
        for p in (PORT_MAIN, PORT_AGG, PORT_PROBE):
            r = self.srv_sh(f"iperf3 -s -p {p} -D --logfile /tmp/iperf_{p}.log --forceflush")
            if r.returncode != 0 and "in use" not in (r.stdout + r.stderr):
                print(f"!! aviso: não consegui iniciar servidor na porta {p}: {(r.stdout + r.stderr).strip()[:120]}")

    def server_offset(self, port):
        r = self.srv_sh(f"wc -c < /tmp/iperf_{port}.log")
        try:
            return int(r.stdout.strip())
        except ValueError:
            return 0

    def cleanup_ue(self):
        run(self.ue(["pkill", "iperf3"]))
        time.sleep(8)

    # ---- verificação do link UE <-> gNB <-> UPF (o UE pode "perder o sinal" sob carga) ----
    def link_ok(self, tries=2):
        a = self.a
        for i in range(tries):
            r = run(self.ue(["ping", "-c", "3", "-W", "2", "-I", a.ue_if, a.dn_ip]), timeout=40)
            if r.returncode == 0:
                return True
            time.sleep(2)
        return False

    def gate(self, flows):
        """Ping ANTES da fase (túnel ocioso). Se falhar, registra as fases como inválidas e retorna False."""
        flows = flows if isinstance(flows, list) else [flows]
        if self.link_ok():
            return True
        for f in flows:
            f.skip("ping pelo túnel falhou ANTES da fase (UE sem sinal?): fase não executada")
        print("!! O UE perdeu o sinal. Reinicie o gNB e depois o UE e repita; os dados coletados até aqui foram salvos.")
        return False

    def finalize(self, flows, check=True):
        """Ping DEPOIS da fase. Se falhar, a(s) fase(s) ficam inválidas (o link caiu durante ou logo após)."""
        flows = flows if isinstance(flows, list) else [flows]
        ok = True
        if check:
            time.sleep(1)
            ok = self.link_ok()
        for f in flows:
            if check and not ok:
                f.valid, f.reason = False, "ping falhou logo APÓS a fase (UE perdeu o sinal durante ou depois)"
                print(f"!! {f.name} marcada como INVÁLIDA: {f.reason}")
            self.record(f)
        if check and not ok:
            print("!! O UE perdeu o sinal. Reinicie o gNB e depois o UE e repita; os dados coletados até aqui foram salvos.")
        return ok

    # ---- execução ----
    def new_run(self, scen):
        a = self.a
        self.rundir = Path(a.out) / f"{datetime.now():%Y%m%d-%H%M%S}_{scen}"
        (self.rundir / "raw").mkdir(parents=True)
        meta = dict(vars(a))
        meta.update(scenario=scen, ue_ip=self.ue_ip, started=time.time())
        json.dump(meta, open(self.rundir / "meta.json", "w"), indent=2, default=str)
        with open(self.rundir / "phases.csv", "w", newline="") as f:
            csv.writer(f).writerow(PHASE_FIELDS)
        print(f">> Execução: {self.rundir}")

    def record(self, fl):
        with open(self.rundir / "phases.csv", "a", newline="") as f:
            csv.writer(f).writerow([
                fl.name, fl.kind, fl.role, fl.rate if fl.rate is not None else "", fl.parallel, fl.dur,
                f"{fl.t_start:.3f}", f"{fl.t_end:.3f}", fl.rc, int(fl.ok),
                f"raw/{fl.name}.json", f"raw/{fl.name}.server.log", int(fl.valid), fl.reason])

    def start_sampler(self):
        a = self.a
        cmd = [sys.executable, str(HERE / "upf_sampler.py"), "--ns", a.upf_ns, "--regex", a.upf_regex,
               "--iface", a.upf_iface, "--out", str(self.rundir), "--extra-ns", a.ns,
               "--extra", f"ue={a.ue_regex}", "--extra", f"gnb={a.gnb_regex}"]
        if a.upf_container:
            cmd += ["--container", a.upf_container]
        self.sampler = subprocess.Popen(cmd)
        time.sleep(4)   # deixa o amostrador anexar nos pods e registrar a linha de base

    def stop_sampler(self):
        if self.sampler:
            time.sleep(3)
            self.sampler.terminate()
            try:
                self.sampler.wait(15)
            except subprocess.TimeoutExpired:
                self.sampler.kill()
            self.sampler = None

    def capacity(self):
        a = self.a
        if a.cap_mbps:
            return float(a.cap_mbps)
        p = Path(a.out) / "capacity.json"
        if p.exists():
            return float(json.load(open(p))["capacity_mbps"])
        die("capacidade C desconhecida: rode 'cap' antes ou defina CAP_MBPS.")


class Flow:
    def __init__(self, ctx, name, port, kind, dur, ip, rate=None, parallel=1, role="main", reverse=False):
        self.ctx, self.name, self.port, self.kind = ctx, name, port, kind
        self.dur, self.ip, self.rate, self.parallel = int(dur), ip, rate, parallel
        self.role, self.reverse = role, reverse
        self.ok, self.rc, self.t_start, self.t_end = False, -1, 0.0, 0.0
        self.valid, self.reason = True, ""

    def skip(self, why):
        """Fase não executada (link fora do ar): registra como inválida."""
        self.valid, self.reason, self.ok, self.rc = False, why, False, -2
        self.t_start = self.t_end = time.time()
        self.ctx.record(self)
        print(f"!! {self.name} NÃO executada: {why}")

    def start(self):
        c, a = self.ctx, self.ctx.a
        raw = c.rundir / "raw"
        self.jpath, self.spath = raw / f"{self.name}.json", raw / f"{self.name}.server.log"
        self.off = c.server_offset(self.port)
        cmd = ["iperf3", "-c", a.dn_ip, "-p", str(self.port), "-B", self.ip, "-i", "1", "-J",
               "-t", str(self.dur)]
        if self.kind == "udp":
            cmd += ["-u", "-b", f"{self.rate}M", "-l", str(a.payload), "--get-server-output"]
        else:
            cmd += ["-P", str(self.parallel)]
        if self.reverse:
            cmd.append("-R")
        if c.has_timeout:
            cmd = ["timeout", str(self.dur + 30)] + cmd
        rate_txt = f" @ {self.rate} Mbps" if self.rate is not None else f" P={self.parallel}"
        print(f">> [{self.role}] {self.name} ({self.kind}{rate_txt}, {self.dur}s)")
        self._fj = open(self.jpath, "w")
        self._fe = open(raw / f"{self.name}.err", "w")
        self.t_start = time.time()
        self.proc = subprocess.Popen(c.ue(cmd), stdout=self._fj, stderr=self._fe)

    def wait(self):
        c = self.ctx
        self.rc = self.proc.wait()
        self.t_end = time.time()
        self._fj.close(); self._fe.close()
        r = c.srv_sh(f"tail -c +{self.off + 1} /tmp/iperf_{self.port}.log")
        self.spath.write_text(r.stdout)
        err = json_error(self.jpath)
        self.ok = self.rc == 0 and not err
        if not self.ok:
            why = "estourou o tempo máximo" if self.rc == 124 else (err or f"exit {self.rc}")
            print(f"!! {self.name} falhou: {why}  (o log do receptor foi salvo e será usado)")
        if not self.ok:
            c.cleanup_ue()
        return self.ok


def parse_phases(spec, default_sec):
    out = []
    for tok in spec.split(","):
        tok = tok.strip()
        if not tok:
            continue
        pct, _, sec = tok.partition(":")
        out.append((float(pct), int(sec) if sec else default_sec))
    return out


def checar_teto(a, valores, nome):
    acima = [v for v in valores if v > a.max_pct]
    if acima and not a.allow_high:
        die(f"{nome} {acima} passa do teto de {a.max_pct:g}% de C (o UE tende a perder o sinal acima disso). "
            f"Ajuste, aumente --max-pct ou use --allow-high.")


def finish(ctx):
    ctx.stop_sampler()
    if not ctx.a.no_plot and ctx.rundir:
        import analyze
        analyze.analyze(ctx.rundir)


# ------------------------------------------------------------------ cenários
def sc_cap(ctx):
    a = ctx.a
    ctx.new_run("cap"); ctx.start_sampler()
    res = {}
    try:
        for n in (1, 4):
            f = Flow(ctx, f"tcp_P{n}", PORT_MAIN, "tcp", a.cap_dur, ctx.ue_ip, parallel=n)
            if not ctx.gate(f):
                break
            f.start()
            ran = f.wait()
            valid = ctx.finalize([f])
            if ran and valid:
                d = json.load(open(f.jpath))
                res[f"P{n}"] = d["end"]["sum_received"]["bits_per_second"] / 1e6
            if not valid:
                break
            time.sleep(a.gap)
    finally:
        finish(ctx)
    if not res:
        die("nenhum teste TCP válido; não foi possível estimar a capacidade.")
    cap = max(res.values())
    json.dump({"capacity_mbps": round(cap, 1), "tcp_mbps": res, "run": str(ctx.rundir), "ts": time.time()},
              open(Path(a.out) / "capacity.json", "w"), indent=2)
    print(f"\n>> Capacidade estimada C = {cap:.1f} Mbps  (TCP: {res})")
    print(f"   Salva em {Path(a.out) / 'capacity.json'}; os cenários seguintes usam esse valor.")


def sc_phases(ctx, scen, spec):
    a = ctx.a
    C = ctx.capacity()
    phases = parse_phases(spec, a.phase_dur)
    checar_teto(a, [pct for pct, _ in phases], "fases")
    total = sum(s for _, s in phases) + a.gap * (len(phases) - 1)
    print(f">> C = {C:.1f} Mbps | fases (% de C : s): {phases}")
    ctx.new_run(scen); ctx.start_sampler()
    probe, aborted = None, False
    try:
        if ctx.probe_ip:
            probe = Flow(ctx, "probe_udp", PORT_PROBE, "udp", total + 4, ctx.probe_ip,
                         rate=a.probe_mbps, role="probe")
            probe.start()
        for i, (pct, sec) in enumerate(phases, 1):
            rate = round(C * pct / 100.0, 1)
            f = Flow(ctx, f"p{i:02d}_udp_{pct:g}pct", PORT_MAIN, "udp", sec, ctx.ue_ip, rate=rate)
            if not ctx.gate(f):
                aborted = True
                break
            f.start(); f.wait()
            if not ctx.finalize([f]):
                aborted = True
                break
            if i < len(phases):
                time.sleep(a.gap)
        if probe:
            if aborted:
                run(ctx.ue(["pkill", "iperf3"]))   # encerra a sonda que ainda estaria rodando
            probe.wait()
            if aborted:
                probe.valid, probe.reason = False, "sonda interrompida pela queda de sinal do UE"
            ctx.finalize([probe], check=False)
    finally:
        finish(ctx)


def sc_tcpsat(ctx):
    a = ctx.a
    C = ctx.capacity()
    checar_teto(a, [a.agg_pct], "agressor")
    agg_rate = round(C * a.agg_pct / 100.0, 1)
    ctx.new_run("tcpsat"); ctx.start_sampler()
    try:
        f = Flow(ctx, "tcp_sozinho", PORT_MAIN, "tcp", a.tcp_dur, ctx.ue_ip, parallel=a.tcp_streams)
        if not ctx.gate(f):
            return
        f.start(); f.wait()
        if not ctx.finalize([f]):
            return
        time.sleep(a.gap + 3)
        agg = Flow(ctx, "udp_agressor", PORT_AGG, "udp", a.tcp_dur + 8, ctx.ue_ip, rate=agg_rate, role="aggressor")
        main = Flow(ctx, "tcp_com_agressor", PORT_MAIN, "tcp", a.tcp_dur, ctx.ue_ip, parallel=a.tcp_streams)
        if not ctx.gate([agg, main]):
            return
        agg.start()
        time.sleep(3)   # o agressor já está saturando quando o TCP começa
        main.start(); main.wait()
        agg.wait()
        ctx.finalize([agg, main])
    finally:
        finish(ctx)


# ------------------------------------------------------------------ preflight
def cmd_preflight(ctx):
    a = ctx.a
    bad = []

    def chk(name, ok, detail=""):
        print(f"[{'OK' if ok else 'FALHOU'}] {name} {detail}")
        if not ok:
            bad.append(name)

    r = run(["kubectl", "get", "nodes"])
    chk("kubectl acessa o cluster", r.returncode == 0, r.stderr.strip()[:100])
    if r.returncode:
        die("configure o KUBECONFIG antes de continuar")
    r = run(ctx.ue(["iperf3", "--version"]))
    chk("iperf3 no pod UE", r.returncode == 0, (r.stdout.splitlines() or [r.stderr.strip()])[0][:70])
    r = run(["kubectl", "-n", a.srv_ns, "get", "pod", a.srv_pod, "-o", "jsonpath={.status.podIP}"])
    a.dn_ip = a.dn_ip or r.stdout.strip()
    chk(f"pod {a.srv_pod}", bool(a.dn_ip), a.dn_ip)
    if not a.dn_ip:
        die("sem IP do servidor")
    ip, out = ctx.ip_of(a.ue_if)
    chk(f"ping pelo túnel {a.ue_if} -> DN", bool(ip), f"(IP do UE: {ip})")
    if not ip:
        print(out); die("sem conectividade pelo túnel")
    ctx.ue_ip = a.ue_ip or ip
    r = run(ctx.ue(["ip", "route", "get", a.dn_ip, "from", ctx.ue_ip]))
    line = (r.stdout.strip().splitlines() or ["(comando 'ip' ausente no pod)"])[0]
    chk("rota do IP do UE sai pelo túnel", a.ue_if in r.stdout, line[:90])
    ctx.has_timeout = run(ctx.ue(["sh", "-c", "command -v timeout"])).returncode == 0
    chk("'timeout' disponível no pod UE", ctx.has_timeout, "(evita testes pendurados)")
    ctx.ensure_servers(); time.sleep(1)
    for port in (PORT_MAIN, PORT_AGG, PORT_PROBE):
        r = run(ctx.ue(["iperf3", "-c", a.dn_ip, "-p", str(port), "-B", ctx.ue_ip, "-t", "2", "-J"]))
        try:
            mbps = json.loads(r.stdout)["end"]["sum_received"]["bits_per_second"] / 1e6
            chk(f"iperf3 TCP 2s na porta {port}", mbps > 0, f"{mbps:.0f} Mbps")
        except Exception:
            chk(f"iperf3 TCP 2s na porta {port}", False, (r.stdout[:80] or r.stderr[:80]))
    r = run(["kubectl", "-n", a.upf_ns, "get", "pods", "-o", "name"])
    pods = [p.split("/")[-1] for p in r.stdout.split() if re.search(a.upf_regex, p, re.I)]
    chk(f"pods da UPF (ns {a.upf_ns}, regex '{a.upf_regex}')", bool(pods), ", ".join(pods))
    for p in pods:
        cmd = ["kubectl", "-n", a.upf_ns, "exec", p] + (["-c", a.upf_container] if a.upf_container else [])
        r = run(cmd + ["--", "sh", "-c", f"read x < /sys/class/net/{a.upf_iface}/statistics/rx_bytes; echo $x"])
        chk(f"contadores de {a.upf_iface} em {p}", r.stdout.strip().isdigit(), r.stdout.strip() or r.stderr.strip()[:80])
    for p in pods:
        cmd = ["kubectl", "-n", a.upf_ns, "exec", p] + (["-c", a.upf_container] if a.upf_container else [])
        r = run(cmd + ["--", "sh", "-c", CPU_SH + 'echo "$cpu $thr $nthr"; echo "cpu.max=$cmax"'])
        ls = r.stdout.splitlines()
        v = ls[0].split() if ls else []
        cmax = ls[1].replace("cpu.max=", "") if len(ls) > 1 else "?"
        chk(f"CPU/throttling do CONTAINER de {p}", len(v) == 3 and v[0].isdigit() and v[0] != "0",
            f"cpu_usec={v[0] if v else '?'} nr_throttled={v[2] if len(v) == 3 else '?'} cpu.max='{cmax}'")
    r = run(["kubectl", "-n", a.ns, "get", "pods", "-o", "name"])
    nomes = [x.split("/")[-1] for x in r.stdout.split()]
    for papel, rgx in (("UE", a.ue_regex), ("gNB", a.gnb_regex)):
        achados = [n for n in nomes if re.search(rgx, n, re.I)]
        chk(f"pods do {papel} (regex '{rgx}')", len(achados) >= 1, ", ".join(achados))
        for n in achados:
            r = run(["kubectl", "-n", a.ns, "exec", n, "--", "sh", "-c", CPU_SH + 'echo "$cpu $thr $nthr"'])
            v = (r.stdout.splitlines() or [""])[0].split()
            chk(f"CPU do CONTAINER de {n}", len(v) == 3 and v[0].isdigit() and v[0] != "0", f"cpu_usec={v[0] if v else '?'}")
    r = run(["kubectl", "-n", a.upf_ns, "get", "hpa"])
    print(f"[INFO] HPA no ns {a.upf_ns}: {r.stdout.strip() or r.stderr.strip() or 'nenhum'}")
    print("\n>> " + ("Tudo certo. Próximo: python3 lab5g.py cap" if not bad else f"Corrija: {', '.join(bad)}"))


def main():
    load_env(env("LAB_ENV", "lab.env"))
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["preflight", "cap", "ladder", "tcpsat", "scale", "plot"])
    ap.add_argument("rundir", nargs="?")
    ns = env("NS", "default")
    D = [("--ns", ns), ("--ue-pod", env("UE_POD", "deployment/ueransim-gnb-ues")),
         ("--ue-container", env("UE_CONTAINER")), ("--ue-if", env("UE_IF", "uesimtun0")),
         ("--ue-ip", env("UE_IP")), ("--probe-if", env("PROBE_IF")),
         ("--srv-pod", env("SERVER_POD", "iperf-server")), ("--srv-ns", env("SERVER_NS", ns)),
         ("--dn-ip", env("DN_IP")), ("--upf-ns", env("UPF_NS", ns)), ("--upf-regex", env("UPF_REGEX", "upf")),
         ("--upf-iface", env("UPF_IFACE", "ogstun")), ("--upf-container", env("UPF_CONTAINER")),
         ("--ue-regex", env("UE_REGEX")), ("--gnb-regex", env("GNB_REGEX")),
         ("--out", env("OUT", "runs")), ("--cap-mbps", env("CAP_MBPS")),
         ("--phases", env("PHASES", "25,50,75,90,100,105"))]
    for k, v in D:
        ap.add_argument(k, default=v)
    for k, v in [("--payload", "1200"), ("--cap-dur", "30"), ("--phase-dur", "30"), ("--gap", "3"),
                 ("--tcp-dur", "60"), ("--tcp-streams", "4")]:
        ap.add_argument(k, type=int, default=int(env(k[2:].replace("-", "_").upper(), v)))
    ap.add_argument("--agg-pct", type=float, default=float(env("AGG_PCT", "75")))
    ap.add_argument("--max-pct", type=float, default=float(env("MAX_PCT", "110")))
    ap.add_argument("--allow-high", action="store_true")
    ap.add_argument("--probe-mbps", type=float, default=float(env("PROBE_MBPS", "2")))
    ap.add_argument("--no-plot", action="store_true")
    a = ap.parse_args()
    nome = a.ue_pod.split("/")[-1]
    if not a.ue_regex:     # pod do UE: deployment/<nome> -> pods "<nome>-<hash>"
        a.ue_regex = f"^{re.escape(nome)}-" if "/" in a.ue_pod else f"^{re.escape(nome)}$"
    if not a.gnb_regex:    # gNB: ueransim-gnb-<hash>, sem pegar o pod do UE (ueransim-gnb-ues-...)
        a.gnb_regex = "^ueransim-gnb-(?!ues)"

    if a.cmd == "plot":
        import analyze
        rd = Path(a.rundir) if a.rundir else sorted(Path(a.out).glob("*_*/meta.json"))[-1].parent
        analyze.analyze(rd)
        return
    ctx = Ctx(a)
    if a.cmd == "preflight":
        return cmd_preflight(ctx)
    ctx.detect()
    ctx.ensure_servers()
    try:
        if a.cmd == "cap":
            sc_cap(ctx)
        elif a.cmd == "ladder":
            sc_phases(ctx, "ladder", a.phases)
        elif a.cmd == "scale":
            sc_phases(ctx, "scale", env("SCALE_PHASES", "50:30,100:120"))
        elif a.cmd == "tcpsat":
            sc_tcpsat(ctx)
    except KeyboardInterrupt:
        print("\n!! interrompido; limpando o iperf3 remanescente no pod UE...")
        run(ctx.ue(["pkill", "iperf3"]))


if __name__ == "__main__":
    main()
