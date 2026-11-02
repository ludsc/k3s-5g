#!/usr/bin/env bash
# Análise: gera gráficos/relatórios a partir dos JSON coletados.
# Uso:  source ~/venv-iperf/bin/activate && ./analyze.sh
set -uo pipefail

DATA="${DATA:-data}"
RES="${RES:-results}"
HERE="$(cd "$(dirname "$0")" && pwd)"

command -v iperfplot >/dev/null || { echo "!! ative o venv: source ~/venv-iperf/bin/activate"; exit 1; }

# --- TCP: UE -> DN (o UE é o emissor, então cwnd/RTT/retransmissões estão no JSON principal) ---
for f in "$DATA"/tcp/ue_to_dn_*.json; do
  [ -e "$f" ] || continue
  n="$(basename "$f" .json)"
  echo ">> iperfplot: $n"
  iperfplot all "$f" --out "$RES/tcp_$n" || echo "!! falhou em $n"
done

# --- TCP: DN -> UE (-R): o emissor é o servidor; usar o JSON do lado do servidor ---
f="$DATA/tcp/dn_to_ue_1flow.json"
if [ -e "$f" ]; then
  python3 - "$f" "$DATA/tcp/dn_to_ue_1flow_sender.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
srv = d.get("server_output_json")
if not srv:
    sys.exit("sem server_output_json em " + sys.argv[1])
json.dump(srv, open(sys.argv[2], "w"))
PY
  [ -e "$DATA/tcp/dn_to_ue_1flow_sender.json" ] && \
    iperfplot all "$DATA/tcp/dn_to_ue_1flow_sender.json" --out "$RES/tcp_dn_to_ue_1flow" \
    || echo "!! pulei DN->UE"
fi

# --- UDP: jitter, perda, fora de ordem ---
if ls "$DATA"/udp/*.json >/dev/null 2>&1; then
  echo ">> UDP"
  python3 "$HERE/plot_udp.py" "$DATA"/udp/*.json --out "$RES/udp"
fi

echo ">> Pronto. Abra $RES/tcp_*/report.html e $RES/udp/*.png"
