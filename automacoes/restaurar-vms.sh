#!/usr/bin/env bash
#
# restaurar-vms.sh — restaura um conjunto criado pelo salvar-vms.sh.
#
# Uso:  ./restaurar-vms.sh [--reverter-discos] [--sim] [NOME_DO_CONJUNTO]
#
#   (sem argumentos)    restaura o conjunto mais recente, mas SÓ se os discos
#                       ainda estiverem exatamente como no momento do save.
#   --reverter-discos   antes de restaurar a RAM, devolve os discos ao estado
#                       guardado no conjunto (sobrescreve os discos atuais!).
#                       É o jeito seguro de "voltar a uma versão anterior".
#   --sim               não pede confirmação (para uso em automação).
#   NOME_DO_CONJUNTO    ex.: 20261102-153000 (ver: ls estados-vms/)

set -u -o pipefail
export LC_ALL=C

if [ "$(id -u)" -ne 0 ]; then
    exec sudo -- "$0" "$@"
fi

VMS=("k3s-master" "k3s-worker1" "k3s-worker2")
DIR_ESTADOS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/estados-vms"
VIRSH=(virsh -c qemu:///system)

erro() { echo "ERRO: $*" >&2; }

REVERTER=0
SIM=0
NOME=""
for arg in "$@"; do
    case "$arg" in
        --reverter-discos) REVERTER=1 ;;
        --sim) SIM=1 ;;
        -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
        -*) erro "opção desconhecida: $arg"; exit 2 ;;
        *) NOME="$arg" ;;
    esac
done

# ------------------------------------------------------ escolhe o conjunto
if [ -n "$NOME" ]; then
    CONJUNTO="$DIR_ESTADOS/$NOME"
else
    CONJUNTO=""
    if [ -d "$DIR_ESTADOS" ]; then
        # só pastas "AAAAMMDD-HHMMSS" (ignora .incompleto e .consumido)
        CONJUNTO=$(find "$DIR_ESTADOS" -mindepth 1 -maxdepth 1 -type d \
            -name '[0-9]*-[0-9]*' ! -name '*.*' | sort | tail -n1)
    fi
fi

if [ -z "$CONJUNTO" ] || [ ! -d "$CONJUNTO" ] || [ ! -f "$CONJUNTO/manifesto.txt" ]; then
    erro "nenhum conjunto válido encontrado${NOME:+ com o nome '$NOME'} em $DIR_ESTADOS."
    exit 1
fi
MANIFESTO="$CONJUNTO/manifesto.txt"
echo "Conjunto: $CONJUNTO"

# ------------------------------------- pré-checagem (tudo ou nada)
falhou=0
for vm in "${VMS[@]}"; do
    if [ ! -s "$CONJUNTO/$vm.save" ]; then
        erro "falta o estado de $vm em $CONJUNTO."
        falhou=1
        continue
    fi

    estado=$("${VIRSH[@]}" domstate "$vm" 2>/dev/null | head -n1)
    if [ "$estado" != "shut off" ]; then
        erro "$vm está '${estado:-desconhecido}'; precisa estar desligada para restaurar."
        falhou=1
        continue
    fi

    while IFS='|' read -r v alvo caminho tam mtime; do
        [ "$v" = "$vm" ] || continue
        if [ "$REVERTER" -eq 1 ]; then
            if [ ! -f "$CONJUNTO/$vm.$alvo.disco" ]; then
                erro "este conjunto não tem cópia do disco $alvo de $vm (foi salvo com --sem-discos)."
                falhou=1
            fi
        else
            atual=$(stat -c '%s %Y' "$caminho" 2>/dev/null)
            if [ "$atual" != "$tam $mtime" ]; then
                erro "o disco $caminho ($vm/$alvo) mudou desde o save."
                erro "  Restaurar a RAM sobre ele corromperia o filesystem."
                erro "  Se o conjunto tem cópia dos discos, use --reverter-discos."
                falhou=1
            fi
        fi
    done < "$MANIFESTO"
done

if [ "$falhou" -ne 0 ]; then
    echo "Nada foi alterado." >&2
    exit 1
fi

# ------------------------------------------------- reverte discos (opcional)
if [ "$REVERTER" -eq 1 ]; then
    if [ "$SIM" -eq 0 ]; then
        echo
        echo "ATENÇÃO: os discos ATUAIS das VMs serão sobrescritos pelas cópias do conjunto."
        echo "Tudo que mudou nelas desde o save será perdido."
        read -r -p "Continuar? [s/N] " resp
        [[ "$resp" =~ ^[sS]$ ]] || { echo "Cancelado."; exit 1; }
    fi

    echo "== Revertendo discos =="
    while IFS='|' read -r vm alvo caminho _ _; do
        echo " - $caminho ($vm/$alvo)"
        if ! cp --reflink=auto --sparse=always -- "$CONJUNTO/$vm.$alvo.disco" "$caminho"; then
            erro "falha ao copiar para $caminho: o disco pode estar incompleto."
            erro "NÃO restaure as VMs. Corrija (espaço/permissão) e rode de novo com --reverter-discos."
            exit 1
        fi
    done < "$MANIFESTO"
fi

# ------------------------------------------------------- restaura a RAM
echo "== Restaurando VMs =="
for vm in "${VMS[@]}"; do
    echo " - $vm..."
    if ! "${VIRSH[@]}" restore "$CONJUNTO/$vm.save"; then
        erro "falha ao restaurar $vm. Interrompendo; as VMs seguintes NÃO foram restauradas."
        erro "Se alguma anterior já subiu, desligue-a (virsh destroy) e rode de novo com --reverter-discos"
        erro "(precisa de conjunto com cópia dos discos)."
        exit 1
    fi
    # reajusta o relógio do guest (precisa do qemu-guest-agent; ignora se não houver)
    "${VIRSH[@]}" domtime "$vm" --sync >/dev/null 2>&1 || true
done

# ------------------------------------------- o que fazer com o conjunto
# Com cópia dos discos ele é um checkpoint reutilizável (use --reverter-discos).
# Sem cópia, depois que as VMs rodam o .save fica obsoleto: marca como consumido.
if compgen -G "$CONJUNTO/*.disco" > /dev/null; then
    echo "Conjunto mantido (tem cópia dos discos): reutilizável com --reverter-discos."
else
    mv -- "$CONJUNTO" "$CONJUNTO.consumido"
    echo "Conjunto marcado como consumido: $CONJUNTO.consumido"
fi

echo "Todas as VMs foram restauradas e estão rodando."
