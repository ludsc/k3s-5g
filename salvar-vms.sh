#!/bin/bash

DIR_ESTADOS="./estados-vms"
VMS=("k3s-master" "k3s-worker1" "k3s-worker2")

# Cria a pasta para guardar os arquivos se ela não existir
mkdir -p "$DIR_ESTADOS"

echo "Iniciando a hibernação das VMs (Save)..."

for vm in "${VMS[@]}"; do
    STATUS=$(sudo virsh domstate $vm 2>/dev/null)
    
    if [ "$STATUS" = "executando" ]; then
        echo "Preparando a VM $vm..."
        
        # 1. Procura se há algum CD-ROM do cloud-init inserido na VM
        CD_DEV=$(sudo virsh domblklist $vm 2>/dev/null | grep 'cloudinit.iso' | awk '{print $1}')
        
        # 2. Se encontrar, ejeta o CD-ROM "ao vivo" (live) antes de salvar
        if [ -n "$CD_DEV" ]; then
            echo " - Ejetando $CD_DEV (cloud-init ISO temporário)..."
            sudo virsh change-media $vm "$CD_DEV" --eject --live >/dev/null 2>&1
        fi

        # 3. Agora salva a VM de forma limpa
        echo " - Salvando a memória da VM no disco..."
        sudo virsh save $vm "$DIR_ESTADOS/$vm.save"
    else
        echo "A VM $vm não está rodando (Status: $STATUS). Ignorando..."
    fi
done

echo "Todas as VMs ativas foram salvas e desligadas com sucesso!"
