# Plan — all-Rust parity with bin/k8s-local-up (allrust-parity_20261123)

*2026-11-23, owner: one shot (gluon all-Rust Stage B, B4). /prompt off.*

## Phase 1: Recipes
- [x] Task: redis:7-alpine captured (capture-laptop-reference.sh) and packaged; tests red `d11e979`
- [x] Task: the redis recipe; tests green `d11e979`
- [x] Task: a Rust port's WORKDIR exists; test red, then green `d11e979`

## Phase 2: up
- [x] Task: implementation per service (`<SERVICE>_IMPL`, `ALL_RUST`), all checked first; tests red, then green `d11e979`
- [x] Task: infra waited for, a Deployment without a pod included; tests red, then green `d11e979`
- [x] Task: switch reset for any service (switch-reset.sql); connector restarted `d11e979`
- [x] Task: connectors registered after the services, every one RUNNING; tests red, then green `d11e979`
- [x] Task: CI green (Linux container) `d11e979`: 281 passed, 5 skipped (CDC jars, a built port), 90.66%
- [ ] Task: in the arm64 bed: all eight ports up (gluon B6)

## Notes
- Red seen: the new unit tests (8 in test_images, test_up not importable); the Redis
  integration test failed `ImageUnavailable` until node.py kept every discovered tool
  (it rebuilt HostTools field by field and dropped `redis_bin`: now `replace`).
- CI ran in the arm64 sandbox bed image, privileged, as model, with Ubuntu's
  redis-server and cargo installed for the run (the fake port's build is `cargo --version`).
- Decision: the default implementation here is rust for every service (bin/k8s-local-up:
  scala unless ALL_RUST=1); the Scala services can't be built in the sandbox (ADR 0028).
