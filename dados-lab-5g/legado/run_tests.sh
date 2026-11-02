#!/usr/bin/env bash
# Coleta: roda os testes iperf3 (TCP e UDP) a partir do pod UE e salva os JSON no host.
# Uso:  NS=default UE_POD=deployment/ueransim-gnb-ues ./run_tests.sh
set -uo pipefail

NS="${NS:?defina NS (namespace)}"
UE_POD="${UE_POD:?defina UE_POD (ex.: deployment/ueransim-gnb-ues)}"
UE_CONTAINER="${UE_CONTAINER:-}"        # só se o pod tiver mais de um container
DN_IP="${DN_IP:-10.42.2.64}"            # IP do pod iperf-server
UE_IF="${UE_IF:-uesimtun0}"             # interface do túnel no UE (usada no ping)
UE_IP="${UE_IP:-}"                      # IP do UE no túnel (se vazio, é detectado pelo ping)
DUR="${DUR:-60}"                        # duração de cada teste (s)
OUT="${OUT:-data}"

mkdir -p "$OUT/tcp" "$OUT/udp"

KC=(kubectl -n "$NS" exec "$UE_POD")
if [ -n "$UE_CONTAINER" ]; then KC+=(-c "$UE_CONTAINER"); fi
ue() { "${KC[@]}" -- "$@"; }

# ---------- 1) conectividade e IP do UE ----------
echo ">> Checando conectividade pelo túnel ($UE_IF -> $DN_IP)"
if ! PING_OUT="$(ue ping -c 3 -I "$UE_IF" "$DN_IP" 2>&1)"; then
  echo "$PING_OUT"
  echo "!! ping falhou pelo túnel. Corrija antes de continuar."; exit 1
fi
echo "$PING_OUT"

if [ -z "$UE_IP" ]; then
  UE_IP="$(echo "$PING_OUT" | grep -m1 '^PING' | sed -n 's/.* from \([0-9.]*\) .*/\1/p')"
fi
if [ -z "$UE_IP" ]; then
  echo "!! não consegui descobrir o IP do UE. Rode com UE_IP=<ip do uesimtun0>."; exit 1
fi
echo ">> IP do UE no túnel: $UE_IP (iperf3 vai usar -B $UE_IP)"

# ---------- 2) função de execução ----------
json_error() {   # imprime o erro do JSON do iperf3 (vazio = sem erro)
  python3 -c 'import json,sys
try:
    print(json.load(open(sys.argv[1])).get("error") or "")
except Exception:
    print("JSON inválido ou vazio")' "$1"
}

run() {   # run <caminho/nome-sem-extensao> <args do iperf3...>
  local name="$1"; shift
  local f="$OUT/$name.json"
  echo ">> $name"
  ue iperf3 -c "$DN_IP" -B "$UE_IP" -i 1 -J "$@" > "$f" 2> "$f.err"
  local rc=$?
  local err; err="$(json_error "$f")"
  if [ $rc -ne 0 ] || [ -n "$err" ]; then
    echo "!! falhou: $name (exit $rc)"
    [ -n "$err" ] && echo "   erro: $err"
    [ -s "$f.err" ] && head -n 5 "$f.err"
    return 1
  fi
  rm -f "$f.err"
  sleep 5   # o servidor iperf3 atende um teste por vez
}

# ---------- 3) testes ----------
# TCP (o primeiro teste é o "smoke test": se falhar, aborta)
run tcp/ue_to_dn_1flow -t "$DUR" || { echo "!! abortando: o primeiro teste falhou"; exit 1; }
run tcp/ue_to_dn_4flows -t "$DUR" -P 4
run tcp/dn_to_ue_1flow  -t "$DUR" -R --get-server-output

# UDP (escada de taxas; -l 1200 evita fragmentação dentro do túnel GTP)
for rate in 50 100 200 300; do
  run "udp/ue_to_dn_${rate}M" -u -b "${rate}M" -l 1200 -t "$DUR" --get-server-output
done

echo ">> Pronto. JSON em $OUT/. Próximo passo: ./analyze.sh"
