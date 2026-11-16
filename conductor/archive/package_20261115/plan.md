# Plan — GK-2 the package (package_20261115)

## Phase 1: The package and CI [checkpoint: 9bd5e01]
- [x] Task: Write gluon `bin/verify-complete-GK-2.sh`; see it red *(gluon file; red 6/6 for the stated reasons)*
- [x] Task: GK-2.1 LICENSE, README, .gitignore, .python-version `918751e`
- [x] Task: GK-2.2 pyproject, `gluon_kube` package and CLI skeleton (tests first) `d1891ec`
- [x] Task: GK-2.4 `scripts/ci.sh` and the GitHub Actions workflow `3b6764c`
- [x] Task: GK-2.3 `tech-stack.md` up to date `8fe4529`
- [x] Task: GK-2.5 gluon: `bin/sandbox-setup` (projects, isolation tools), `tools/README.md` *(gluon files, no sandbox git: `image` checks bwrap, unshare, nsenter and `unshare -Ur`; `projects` syncs tools/gluon-kube; MISSING path proven with a bare PATH)*
- [ ] Task: `bin/verify-complete-GK-2.sh` all PASS but the archive; archive the track
- [x] Task: Conductor - User Manual Verification 'The package and CI' (Protocol in workflow.md)
