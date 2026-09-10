#!/usr/bin/env bash
# 故意复用 A 节点 IP/rank，并使用非法端口；只用于静态审计。
source ./common.env
vllm serve /placeholder/model \
  --data-parallel-size 16 \
  --data-parallel-size-local 8 \
  --data-parallel-start-rank 0 \
  --data-parallel-address 192.0.2.18 \
  --data-parallel-rpc-port 16700 \
  --port 70000
