# isfdb-adapter

A JSON API fronting a self-hosted mirror of ISFDB (Internet Speculative
Fiction Database) — see this repo's own `README.md` for the full pitch,
architecture, and API contract. That README is the primary source of truth
for anyone (including a fresh Claude instance) picking this up; this file
just covers the parts a README wouldn't.

## Relationship to other projects

Built as the backend for [Librarium](../librarium/)'s ISFDB metadata
provider (`../librarium/librarium-api/internal/providers/books/isfdb.go`).
Standalone otherwise — no dependency the other direction, and generic
enough that anything else wanting ISFDB data could use it directly.

## Where this actually runs

This repo (`ennui2342/isfdb-adapter`, public, standalone) is the **only**
copy of the source — deployed straight into the user's k8s homelab cluster
by cloning it at build time, same pattern as the Librarium forks (see
`../librarium/CLAUDE.md`'s "Where this actually runs" section). No source is
embedded in the k8s repo; only the deployment manifests and SOPS-encrypted
config live there.

- Image is **locally built and imported into node containerd**
  (`ghcr.io/ennui2342/isfdb-mirror:local`, `imagePullPolicy: Never`), not
  pulled from a registry.
- Manifests: k8s repo `isfdb/adapter-deployment.yaml`, `refresh-cronjob.yaml`.
  Config (DB creds, ISFDB wiki login) is `isfdb/isfdb-secret.yaml`, SOPS-encrypted.
- See the k8s repo's `RUNBOOK.md`, section "ISFDB mirror", for the exact
  clone/build/import/redeploy commands, and its `CLAUDE.md`'s isfdb-mirror
  table row for the running-service picture (namespace, CronJob schedule).
- Deploy loop when this repo changes: edit → rebuild → `docker save` → `scp`
  to master → `scp` to both workers → `k3s ctr images import` on each worker
  → `kubectl rollout restart` (adapter) / next scheduled run (refresh CronJob).
  There used to be a second, embedded copy of this source directly in the
  k8s repo (predating this repo's existence as a public project) — it's been
  removed now that this repo is the real thing, so there's nothing left to
  keep in sync.

## License

AGPL-3.0. If you fork *this* and run a modified version as a network
service, the AGPL requires making your modified source available to that
service's users.
