# VPS readiness snapshot

Read-only inspection on 2026-09-18 confirmed the proposed deployment shape fits
the current host. This is time-specific evidence, not deployment proof.

## Observed target

- Ubuntu 24.04.4 LTS on x86_64
- Docker 29.3.1 and Docker Compose 5.1.1
- 91 GB free disk space and about 10 GB available memory
- Docker access available to the `sunteak` operator
- ports 8000, 8049, 8800 and 8810 were free at inspection time
- PostgreSQL, Redis, nginx, Tailscale and several protected applications are active
- the current tailnet-only HTTPS root proxies `127.0.0.1:8790`

Two unrelated containers were already unhealthy/restarting during inspection.
They were not modified. Their pre-existing state must not be attributed to or
"fixed" as part of botDCA deployment.

## Selected layout

- project directory: a new isolated directory under `/home/sunteak/apps`
- Compose project: unique botDCA project name and network
- API: `127.0.0.1:8000`, never a public bind
- operator access: separate tailnet-only HTTPS listener, proposed port 8443
- database: project-scoped PostgreSQL volume
- Bybit credentials: UI submission, encrypted vault volume, master key mounted separately
- secrets: operator token, database password/URL and vault key mounted read-only
- process: unprivileged UID/GID matched to the `0600` secret owner at build time,
  with UID/GID 10001 as the portable default; read-only root filesystem and dropped capabilities
- supervision: Compose restart policy plus API and database health checks

The existing Tailscale root route, Crypto Quant port 8790, Sonosole, Nadiatour,
BusinessApp, PostgreSQL, Redis and existing Docker projects are outside botDCA's
deployment scope.

## Deployment gates

Before writing to the VPS:

1. Recheck ports, running services, disk, Docker projects and the existing Tailscale route.
2. Validate the merged Compose configuration on the VPS.
3. Create an isolated project directory and secret files with mode 0600.
4. Start with all live/worker/preflight flags false.
5. Confirm both containers healthy, API bound only to loopback and no protected service changed.
6. Add the separate tailnet HTTPS listener without replacing the existing root route.
7. Validate and encrypt the restricted Bybit key through the UI; place no order.
8. Restart once and prove encrypted credential recovery while the worker remains stopped.

## Deployment result

On 2026-09-18 the operator explicitly retired Crypto Quant and authorized this
deployment. Before shutdown, its strategy runtime was stopped/shadow with zero
nonzero strategy positions; Copy was safety-paused, reconciliation was idle and
its persisted Copy orders were only Filled or Cancelled. The main service and
its retention timer were then stopped and disabled. Its backup timer and testnet
service were already disabled. Port 8790 and Crypto Quant processes were gone,
while the other protected services stayed active.

botDCA release `20260918T065503Z` was installed under
`/home/sunteak/apps/botdca/releases`, with `current` pointing to that release.
The `botdca-api-1` and `botdca-db-1` containers passed health checks. The API is
bound only to `127.0.0.1:8000`, runs as an unprivileged user, has a read-only
root filesystem and all capabilities dropped. A controlled API restart returned
to preview mode with PostgreSQL journal access intact. Live, worker startup and
mainnet preflight remained false; no Bybit credentials were stored and no order
was sent.

The remaining access blocker is privileged Tailscale Serve configuration. The
old root route still targets stopped port 8790 and returns 502. The operator must
run the root-authorized route update to `http://127.0.0.1:8000`; no weaker public
HTTP exposure was added as a workaround.

Funded activation is a later, separately approved operation. It requires a fresh
account/position/order check and explicit approval of the exact configuration.
