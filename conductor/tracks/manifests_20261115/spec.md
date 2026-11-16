# Spec — GK-3 reading the manifests (manifests_20261115)

Source: gluon ADR 0030 decisions 2 and 5; gluon `plans/gluon-kube/plan.md`
GK-3.1–GK-3.7. Nothing runs yet: this is the desired state, read exactly.

## What is built (package `gluon_kube`)
- **Model** (GK-3.1): frozen dataclasses for the simulated kinds (Namespace,
  Deployment, Service, ConfigMap, Job, CronJob) and what they hold (pod template,
  containers, env, ports, probes, volumes and mounts, resources), from the fields
  `infra/k8s/local-infra/` and the chart's output use. Other kinds in `infra/k8s`
  (DaemonSet, Role, RoleBinding, ServiceAccount, Kustomization, …) are recognised
  and reported as not simulated.
- **Loader** (GK-3.2): a file or a directory of multi-document YAML. Every field of
  every object is accounted for: simulated, recorded but not enforced (resources,
  `imagePullPolicy`, `defaultMode`), or unsupported. An unsupported field is reported
  with its object and path, and that object won't be started (GK-4); nothing is
  dropped silently. Malformed input (not a mapping, no kind, wrong types) is an
  error naming the file and document.
- **Values** (GK-3.3): Helm's merge: the chart's `values.yaml`, then each `-f` file in
  order; maps merge recursively, lists and scalars replace, `null` deletes a key.
- **Renderer** (GK-3.4): the Go-template subset `infra/k8s/gluon` uses: text and
  `{{ }}` actions with `{{-`/`-}}` trimming and comments; `.`, `.Release.Name`,
  `.Release.Namespace`, `.Values.…` and `.field` on the current dot; pipelines into
  `quote`, `toYaml`, `nindent N`, `indent N`; `with`/`range`/`if` … `else` … `end` with
  Go's notion of empty. Anything else fails with the template, line and construct.
  `toYaml` as Helm's (sorted keys, no trailing newline).
- **Chart** (GK-3.4): `Chart.yaml`, `values.yaml`, `templates/*.yaml`, rendered per
  release and namespace into documents.
- **CLI** (GK-3.6, 3.7): `gluon-kube template RELEASE CHART [-f VALUES …] [--namespace
  NS]` prints the documents as Helm does (`---`, `# Source:`); `gluon-kube apply
  --dry-run -f PATH …` prints, per object, what would run, what is recorded only and
  what isn't simulated, and exits 1 if anything is unsupported. `apply` without
  `--dry-run` says the runtime comes in GK-4.

## The reference (GK-3.5, GK-3.6)
`helm template` output captured on the laptop (`scripts/capture-laptop-reference.sh`)
goes into `tests/fixtures/helm-template/` with its provenance; a test compares
`gluon-kube template` with each capture as parsed documents. Until it arrives, the
renderer is tested against expectations derived by hand from Helm's documented
behaviour, named as such, and the comparison test skips saying it waits for GK-3.5.

## Acceptance (agreed before the work; `bin/verify-complete-GK-3.sh` in gluon)
1. `tools/gluon-kube` `scripts/ci.sh` green (coverage ≥ 80%)
2. `gluon-kube apply --dry-run -f infra/k8s/local-infra/` exits 0: all 36 objects
   listed, none unsupported
3. every `environments/local/*.values.yaml` renders with `gluon-kube template`
   (release names as `bin/k8s-local-up`), and each output loads with
   `apply --dry-run` with nothing unsupported
4. the renderer refuses a construct it doesn't support (a scratch chart using
   `include`): non-zero exit naming the template, line and construct
5. the template output equals the laptop captures, as parsed documents; or, with
   no capture yet, `plans/gluon-kube/plan.md` records GK-3.5 as waiting (WARN)
6. `bin/verify-complete-kube-spike.sh`: GK-3 PASS, nothing FAIL
7. the track is archived; `tools/gluon-kube` has nothing uncommitted
All PASS: runs `bin/sandbox-save`.

*2026-11-15, at the owner's request:* each criterion is PASS (done, works),
PENDING (not done yet, saying what it waits for) or FAIL (done, broken: a
regression). The overall status is PASS when nothing FAILs (exit 0); GK-3 is
complete, and the session saved, only when all seven PASS.
