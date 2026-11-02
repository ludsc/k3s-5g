# Laboratório 5G Privado: K3s + Open5GS + UERANSIM

Este repositório contém a infraestrutura como código e scripts de automação para subir uma rede Core 5G (Open5GS) e simular antenas/celulares (UERANSIM) rodando sobre um cluster Kubernetes (K3s) no Fedora/KVM.

## 1. Gerenciamento das VMs (Libvirt/KVM)
Comandos e scripts para gerenciar o estado do laboratório sem perder dados.

* **Salvar estado das VMs (Hibernação):**
  O script ejetará ISOs temporárias e salvará a RAM no disco.
  `./salvar-vms.sh`

* **Restaurar estado das VMs:**
  Acorda as VMs exatamente de onde pararam. O K3s voltará automaticamente.
  `./restaurar-vms.sh`

## 2. Otimização de Memória (Host e Master)
O ambiente 5G consome bastante RAM. Caso precise liberar cache ou usar Swap:

* **Liberar Cache do Linux (Rodar no Host):**
  `sudo sync; echo 3 | sudo tee /proc/sys/vm/drop_caches`

* **Criar Swap de Emergência (Rodar no Master se o Helm travar):**
  ```bash
  sudo fallocate -l 2G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile
  sudo swapon /swapfile
  ```

## 3. Kubernetes (`kubectl`)
O `kubectl` é a ferramenta para conversar com o cluster. *Sempre execute no Master.*

* **Configurar permissão (Para rodar sem `sudo`):**
  ```bash
  mkdir -p ~/.kube
  sudo cp /etc/rancher/k3s/k3s.yaml ~/.kube/config
  sudo chown -R $USER:$USER ~/.kube
  chmod 600 ~/.kube/config
  mkdir -p ~/.kube
  ```
* **Configurar ambiente para dar comandos de forma remota""==**
  ```bash
  ssh USUARIO@192.168.121.50 "sudo cat /etc/rancher/k3s/k3s.yaml" > ~/.kube/k3s.yaml
  sed -i 's/127.0.0.1/192.168.121.50/' ~/.kube/k3s.yaml
  export KUBECONFIG=~/.kube/k3s.yaml
  kubectl get nodes
  ```

* **Comandos Básicos:**
  * `kubectl get nodes` -> Lista os servidores do cluster (Master e Workers).
  * `kubectl get pods -A` -> Lista todos os serviços rodando em todos os namespaces.
  * `watch kubectl get pods -o wide` -> Monitora a criação dos pods em tempo real.
  * `kubectl describe pod <nome-do-pod>` -> Mostra detalhes e erros de um pod específico.

* **Troubleshooting (Logs e Acesso):**
  * `kubectl logs deployment/<nome-do-deployment> -f` -> Acompanha os logs em tempo real (Ex: `open5gs-amf`).
  * `kubectl exec -ti <nome-do-pod> -- bash` -> Abre um terminal dentro do container.

## 4. Helm (Gerenciador de Pacotes)
O Helm instala as aplicações (Charts) no Kubernetes de forma automatizada.

* **Baixar e Atualizar Dependências (Charts locais):**
  ```bash
  git clone https://github.com/Gradiant/5g-charts.git
  cd 5g-charts/charts/open5gs
  helm dependency update
  ```

* **Instalar uma Aplicação:**
  `helm install <nome-da-release> <caminho-do-chart> -f <arquivo-de-valores.yaml>`
  *Exemplo:* `helm install open5gs ./5g-charts/charts/open5gs -f 5gSA-values.yaml`

* **Desinstalar uma Aplicação:**
  `helm uninstall <nome-da-release>`

* **Listar Aplicações Instaladas:**
  `helm list`

## 5. Testes de Rede 5G (Comandos Práticos)
Após instalar o Open5GS e o UERANSIM, utilize estes comandos para validar a comunicação.

* **Verificar o Cérebro da Rede (AMF e SMF):**
  ```bash
  kubectl logs deployment/open5gs-amf -f
  kubectl logs deployment/open5gs-smf -f
  ```

* **Verificar a Antena (gNodeB):**
  ```bash
  kubectl logs deployment/uerasim-ueransim-gnb -f
  ```

* **Testar conectividade dos Celulares Simulados (UE):**
  Checar se a placa virtual (túnel 5G) foi criada:
  ```bash
  kubectl exec deployment/uerasim-ueransim-gnb-ues -ti -- ip addr
  ```
  Testar a internet saindo pelo túnel do celular simulado:
  ```bash
  kubectl exec deployment/uerasim-ueransim-gnb-ues -ti -- ping -c 4 8.8.8.8 -I uesimtun0
  ```
