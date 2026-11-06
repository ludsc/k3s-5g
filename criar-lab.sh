#!/bin/bash

# Baixa a imagem do Debian 12 (caso você ainda não tenha)
IMAGE_PATH="/var/lib/libvirt/images/debian-12-cloud.qcow2"
if [ ! -f "$IMAGE_PATH" ]; then
    echo "Baixando imagem do Debian 12..."
    sudo wget https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-generic-amd64.qcow2 -O $IMAGE_PATH
fi

# Função para criar e iniciar uma VM
criar_vm() {
    NOME=$1
    RAM=$2
    VCPU=$3
    IP=$4

    echo "Criando VM: $NOME com IP $IP..."

    # Gera o user-data específico para esta VM (agora com hostname!)
    cat <<EOF > user-data-$NOME.yaml
#cloud-config
hostname: $NOME
manage_etc_hosts: true
password: "k3s"
chpasswd: { expire: False }
ssh_pwauth: True
packages:
  - curl
  - wget
  - vim
  - git
runcmd:
  - curl -fsSL -o get_helm.sh https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3
  - bash get_helm.sh
EOF

    # Gera a configuração de rede específica para esta VM (com a placa enp1s0)
    cat <<EOF > network-$NOME.yaml
version: 2
ethernets:
  enp1s0:
    dhcp4: false
    addresses:
      - $IP/24
    gateway4: 192.168.121.1
    nameservers:
      addresses: [8.8.8.8, 1.1.1.1]
EOF

    # Instancia a VM apontando para os arquivos yaml específicos dela
    virt-install \
      --name $NOME \
      --memory $RAM \
      --vcpus $VCPU \
      --disk size=10,backing_store=$IMAGE_PATH,bus=virtio \
      --os-variant debian12 \
      --network network=default,model=virtio \
      --cloud-init user-data=user-data-$NOME.yaml,network-config=network-$NOME.yaml \
      --import \
      --noautoconsole
      --vcpus $VCPU \
      --cpu host-passthrough \
}

# Criando as 3 VMs (Nome, RAM, vCPU, IP)
criar_vm "k3s-master"  1536 2 "192.168.121.50"
criar_vm "k3s-worker1" 1536 2 "192.168.121.51"
criar_vm "k3s-worker2" 1536 2 "192.168.121.52"

echo "Todas as VMs foram iniciadas! Aguarde uns 2 minutos para o cloud-init finalizar as instalações."
