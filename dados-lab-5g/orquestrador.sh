#!/bin/bash
echo "checando capacidade"
python3 lab5g.py cap
echo "aguardando pra nao sobrecarregar máquina"
sleep 10
python3 lab5g.py ladder
echo "aguardando pra nao sobrecarregar máquina 2min $date"
sleep 120
python3 lab5g.py tcpsat
echo "FINALIZADO $date"
