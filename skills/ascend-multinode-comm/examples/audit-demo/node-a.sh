#!/usr/bin/env bash
# 只用于静态审计演示，不要执行。
source ./common.env
vllm serve /placeholder/model \
  --data-parallel-size 16 \
  --data-parallel-size-local 8 \
  --data-parallel-start-rank 0 \
  --data-parallel-address 192.0.2.18 \
  --data-parallel-rpc-port 16700 \
  --port 8000
