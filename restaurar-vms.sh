#!/bin/bash

DIR_ESTADOS="./estados-vms"
VMS=("k3s-master" "k3s-worker1" "k3s-worker2")

echo "Iniciando a restauração das VMs..."

for vm in "${VMS[@]}"; do
    ARQUIVO="$DIR_ESTADOS/$vm.save"
    
    if [ -f "$ARQUIVO" ]; then
        echo "Restaurando a VM: $vm..."
        # O comando restore carrega a RAM de volta e apaga o arquivo .save (opcionalmente)
        sudo virsh restore "$ARQUIVO"
        
        # Move o arquivo para evitar tentar restaurar um estado obsoleto no futuro
        mv "$ARQUIVO" "$ARQUIVO.old"
    else
        echo "Nenhum estado salvo encontrado para $vm. Ignorando..."
    fi
done

echo "Todas as VMs possíveis foram restauradas e estão rodando!"
