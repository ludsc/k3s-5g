#!/usr/bin/env bash
#
# salvar-vms.sh — hiberna as VMs do cluster k3s guardando, num mesmo
# "conjunto" (pasta com data/hora), a RAM de cada VM (.save) junto com o
# registro (e, por padrão, uma cópia) dos discos no instante do save.
#
# Por quê: o .save só é válido para o disco EXATAMENTE como ele estava no
# momento do save. Guardar os dois juntos permite detectar (ou desfazer) uma
# divergência antes de ela corromper o filesystem.
#
# Uso:  ./salvar-vms.sh [--sem-discos]
#   --sem-discos  não copia os discos (só registra tamanho/mtime). Mais rápido
#                 e sem gastar espaço, mas o conjunto só serve para UMA
#                 restauração, e só se os discos não forem tocados antes.

set -u -o pipefail
export LC_ALL=C   # saída do virsh em inglês, independente do locale

# Precisa de root (virsh no libvirt do sistema, cópia dos discos)
if [ "$(id -u)" -ne 0 ]; then
    exec sudo -- "$0" "$@"
fi

VMS=("k3s-master" "k3s-worker1" "k3s-worker2")
DIR_ESTADOS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/estados-vms"
VIRSH=(virsh -c qemu:///system)

COPIAR_DISCOS=1
case "${1:-}" in
    "") ;;
    --sem-discos) COPIAR_DISCOS=0 ;;
    *) echo "Uso: $0 [--sem-discos]" >&2; exit 2 ;;
esac

erro() { echo "ERRO: $*" >&2; }

estado_da_vm() { "${VIRSH[@]}" domstate "$1" 2>/dev/null | head -n1; }

# "alvo|caminho" de cada disco (não CD-ROM) que tem arquivo
discos_da_vm() {
    "${VIRSH[@]}" domblklist "$1" --details 2>/dev/null |
        awk '$2=="disk" && $4!="-" {print $3 "|" $4}'
}

# alvos de CD-ROM que ainda têm mídia inserida
cdroms_com_midia() {
    "${VIRSH[@]}" domblklist "$1" --details 2>/dev/null |
        awk '$2=="cdrom" && $4!="-" {print $3}'
}

mkdir -p "$DIR_ESTADOS"

# ---------------------------------------------------------------- pré-checagem
echo "== Verificando VMs =="
for vm in "${VMS[@]}"; do
    estado=$(estado_da_vm "$vm")
    if [ "$estado" != "running" ]; then
        erro "$vm não está rodando (estado: ${estado:-desconhecido})."
        erro "Um conjunto precisa das ${#VMS[@]} VMs juntas (é um cluster). Nada foi feito."
        exit 1
    fi
done

# Espaço: cada .save ocupa ~ a RAM usada pela VM
precisa_kib=0
for vm in "${VMS[@]}"; do
    kib=$("${VIRSH[@]}" dominfo "$vm" 2>/dev/null | awk '/^Used memory:/ {print $3}')
    precisa_kib=$(( precisa_kib + ${kib:-0} ))
done
livre_kib=$(df --output=avail -k "$DIR_ESTADOS" | tail -n1 | tr -d ' ')
if [ "$livre_kib" -lt "$precisa_kib" ]; then
    erro "espaço insuficiente em $DIR_ESTADOS (livre: ${livre_kib} KiB, necessário: ~${precisa_kib} KiB)."
    exit 1
fi

# CD-ROM do cloud-init: precisa estar ejetado antes do save, senão o restore
# depende de um ISO que pode ter sumido. Se não ejetar, NÃO salva.
echo "== Ejetando CD-ROMs =="
for vm in "${VMS[@]}"; do
    while read -r alvo; do
        [ -n "$alvo" ] || continue
        echo " - $vm: ejetando $alvo..."
        "${VIRSH[@]}" change-media "$vm" "$alvo" --eject --live >/dev/null 2>&1
    done < <(cdroms_com_midia "$vm")

    if [ -n "$(cdroms_com_midia "$vm")" ]; then
        erro "não consegui ejetar o CD-ROM de $vm (o guest pode ter travado a bandeja)."
        erro "Dentro da VM rode 'sudo eject' e tente de novo. Nada foi salvo."
        exit 1
    fi
done

# ------------------------------------------------------------------ conjunto
NOME="$(date +%Y%m%d-%H%M%S)"
TMP="$DIR_ESTADOS/$NOME.incompleto"   # só vira "$NOME" se TUDO der certo
DESTINO="$DIR_ESTADOS/$NOME"
mkdir -p "$TMP"

declare -A DISCOS
SALVAS=()

# Se algo falhar no meio, religa as VMs já salvas (os discos delas não foram
# tocados, então o .save ainda é válido) e descarta o conjunto incompleto.
desfazer() {
    erro "$1"
    echo "Revertendo: religando as VMs que já tinham sido salvas..." >&2
    local ok=1 v
    for v in "${SALVAS[@]}"; do
        if "${VIRSH[@]}" restore "$TMP/$v.save"; then
            rm -f -- "$TMP/$v.save"   # a VM voltou a rodar: esse .save ficou obsoleto
        else
            ok=0
            erro "não consegui religar $v; o estado dela continua em $TMP/$v.save"
        fi
    done
    if [ "$ok" -eq 1 ]; then
        rm -rf -- "$TMP"
        echo "Cluster religado; conjunto descartado." >&2
    fi
    exit 1
}

# --------------------------------------------------------- 1) salva a RAM
# Primeiro salva TODAS (rápido, pouco desvio de relógio entre as VMs),
# só depois mexe nos discos.
echo "== Salvando a RAM das VMs =="
for vm in "${VMS[@]}"; do
    DISCOS[$vm]="$(discos_da_vm "$vm")"   # lista os discos com a VM ainda rodando
    echo " - salvando $vm..."
    if ! "${VIRSH[@]}" save "$vm" "$TMP/$vm.save.parcial"; then
        rm -f -- "$TMP/$vm.save.parcial"
        desfazer "falha ao salvar $vm"
    fi
    if [ ! -s "$TMP/$vm.save.parcial" ]; then
        rm -f -- "$TMP/$vm.save.parcial"
        desfazer "arquivo de estado de $vm saiu vazio"
    fi
    mv -- "$TMP/$vm.save.parcial" "$TMP/$vm.save"
    SALVAS+=("$vm")
    "${VIRSH[@]}" dumpxml --inactive "$vm" > "$TMP/$vm.xml" 2>/dev/null
done

# ------------------------------------------- 2) registra / copia os discos
# Agora todas as VMs estão desligadas: os discos estão parados.
echo "== Registrando discos =="
MANIFESTO="$TMP/manifesto.txt"
: > "$MANIFESTO"
for vm in "${VMS[@]}"; do
    while IFS='|' read -r alvo caminho; do
        [ -n "$caminho" ] || continue
        info=$(stat -c '%s %Y' "$caminho") || desfazer "não consegui ler $caminho"
        tam=${info% *}
        mtime=${info#* }
        echo "$vm|$alvo|$caminho|$tam|$mtime" >> "$MANIFESTO"

        if [ "$COPIAR_DISCOS" -eq 1 ]; then
            echo " - copiando $caminho ($vm/$alvo)..."
            cp --reflink=auto --sparse=always -- "$caminho" "$TMP/$vm.$alvo.disco" \
                || desfazer "falha ao copiar o disco $caminho (espaço?)"
        fi
    done <<< "${DISCOS[$vm]}"
done

sync
mv -- "$TMP" "$DESTINO"

echo
echo "Conjunto salvo em: $DESTINO"
if [ "$COPIAR_DISCOS" -eq 1 ]; then
    echo "Inclui cópia dos discos: pode ser restaurado depois com --reverter-discos."
else
    echo "SEM cópia dos discos: restaure antes de mexer em qualquer disco/VM."
fi
echo "Todas as VMs foram salvas e estão desligadas."
