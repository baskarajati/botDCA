# VPS mainnet credential vault

## Objective

Allow the operator to enter a Bybit mainnet API key and secret from the
Configuration page without storing either value in browser storage, Git, `.env`,
PostgreSQL or logs. Credentials must survive a VPS/container restart so an open
position can be reconciled and managed after recovery.

This feature prepares mainnet credentials. It does not arm the worker, enable
entries or place an order. Mainnet activation remains a separate explicit step.

## Storage architecture

The VPS deployment supplies a random Fernet master key as a read-only Docker
secret at `/run/secrets/botdca_credential_key`. The application encrypts the
submitted Bybit credentials and stores only the authenticated ciphertext in a
dedicated persistent volume at `/var/lib/botdca/secrets/bybit.enc`.

The deployment key and ciphertext therefore live in separate storage. The
application never returns the API key or secret after submission. A restart
loads and decrypts the credential pair before evaluating live-worker startup
gates. Environment credentials remain supported for backward compatibility but
are not the recommended VPS path.

Encryption does not protect against a VPS root compromise. File permissions,
private HTTPS access, host patching, firewall policy and Bybit IP restrictions
remain required.

## Submission and validation

The Configuration page collects API key, API secret and the exact confirmation
phrase `MAINNET`. The request requires the operator token and secure transport,
except loopback development. Fields are cleared immediately after submission.

Before storage, the backend calls Bybit mainnet read-only endpoints and requires:

- a read/write key;
- Unified Trading Account status;
- Contract Trade `Order` and `Position` permissions;
- at least one bound IP address;
- no Wallet permissions;
- successful wallet and configured-symbol position reads;
- one-way position mode.

Failures return actionable but credential-free messages. The response contains
only validation time, masked key fingerprint, IP-binding count, UTA state and
permission status. Credential rotation is blocked while a worker is running.

## VPS deployment

The container runs as an unprivileged application user. A Compose VPS override
mounts the deployment key as a service-scoped secret and mounts a named volume
for the encrypted blob. The API remains bound to loopback; an approved private
HTTPS/Tailscale or reverse-proxy path is required for remote access.

The master-key source file is created on the VPS with mode `0600`, outside Git.
Losing it makes the encrypted credential blob unrecoverable. Back it up in the
operator's secret manager, not alongside database exports.

## Verification

Tests cover encrypted round trips, atomic file permissions, wrong-key failure,
validation rejection, sanitized responses, credential-free audit payloads,
restart loading and worker-construction compatibility. Browser checks cover
labels, error recovery, field clearing, locked state and mobile layout. No real
key or exchange order is used during implementation verification.
