#!/bin/bash

DIR_ESTADOS="./estados-vms"
VMS=("k3s-master" "k3s-worker1" "k3s-worker2")

# Cria a pasta para guardar os arquivos se ela não existir
mkdir -p "$DIR_ESTADOS"

echo "Iniciando a hibernação das VMs (Save)..."

for vm in "${VMS[@]}"; do
    STATUS=$(sudo virsh domstate $vm 2>/dev/null)
    
    if [ "$STATUS" = "executando" ]; then
        echo "Salvando o estado da VM: $vm..."
        # O comando save grava a RAM no disco e desliga a VM
        sudo virsh save $vm --file "$DIR_ESTADOS/$vm.save"
    else
        echo "A VM $vm não está rodando (Status: $STATUS). Ignorando..."
    fi
done

echo "Todas as VMs ativas foram salvas e desligadas!"
