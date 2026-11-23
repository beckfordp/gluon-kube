# Plan — all-Rust parity with bin/k8s-local-up (allrust-parity_20261123)

*2026-11-23, owner: one shot (gluon all-Rust Stage B, B4). /prompt off.*

## Phase 1: Recipes
- [ ] Task: redis:7-alpine captured (capture-laptop-reference.sh) and packaged; tests red
- [ ] Task: the redis recipe; tests green
- [ ] Task: a Rust port's WORKDIR exists; test red, then green

## Phase 2: up
- [ ] Task: implementation per service (`<SERVICE>_IMPL`, `ALL_RUST`), all checked first; tests red, then green
- [ ] Task: infra waited for, a Deployment without a pod included; tests red, then green
- [ ] Task: switch reset for any service (switch-reset.sql); connector restarted
- [ ] Task: connectors registered after the services, every one RUNNING; tests red, then green
- [ ] Task: CI green (Linux container); in the arm64 bed: all eight ports up
