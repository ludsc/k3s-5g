#!/bin/bash

echo "desinstalando o cluster open5gs e apagando o banco de dados"
helm uninstall open5gs 
kubectl delete pvc --all
sleep 1
echo "desinstalando o ueransim"
helm uninstall ueransim-gnb
sleep 1

echo "atualizando dependências"
helm dependency update 5g-charts/charts/open5gs
sleep 5

echo "instalando cluster open5gs"
helm install open5gs 5g-charts/charts/open5gs -f 5gSA-values.yaml --wait --timeout 10m

echo "aguardando o core estabilizar (populate, associação PFCP)"
sleep 45

echo "instalando ueransim"
helm install ueransim-gnb 5g-charts/charts/ueransim-gnb/ -f gnb-ues-values.yaml --wait --timeout 5m

sleep 20
kubectl logs deployment/ueransim-gnb-ues | grep -i "tun interface"
