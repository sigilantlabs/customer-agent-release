# Customer agent release runbook

This repository publishes only three customer-facing assets: the installer,
the signed release manifest, and its Ed25519 signature. It never receives the
Sigilant product source, private evaluation sets, customer traces, model
ranking policy, or the customer-runtime build context.

## One-time repository and package gates

1. Keep `sigilantlabs/customer-agent-release` minimal and make it public. A
   GitHub Release in a private repository is not a public installer origin.
2. Build the container in `sigilantlabs/sigilant-optimizer` with
   `.github/workflows/build-customer-agent.yml`. That workflow uses its own
   repository-scoped `GITHUB_TOKEN`; it does not use a PAT.
3. Make only the `sigilantlabs/customer-agent` GHCR package public. This is an
   irreversible GitHub setting, so it remains an explicit organization-owner
   launch decision. Do not make the source repository public.
4. Add `SIGILANT_RELEASE_PRIVATE_KEY` and `SIGILANT_RELEASE_KEY_ID` as Actions
   secrets here. Add the production control plane as the
   `SIGILANT_CONTROL_URL` Actions variable.

The publication workflow deliberately does not authenticate to GHCR. Its
unauthenticated pull proves the same immutable image is available to a fresh
customer machine with no registry credential.

## Per-release handoff

1. Record the exact image reference emitted by the build workflow. It must be
   `ghcr.io/sigilantlabs/customer-agent@sha256:<64 lowercase hex>`.
2. Pull that exact digest on the approved GCP L4 host and run the qualification
   suite. Do not rebuild or retag the bytes during qualification.
3. Add the reviewed qualification JSON under `qualifications/`. Its `image` or
   `image_ref` value must equal the exact digest above. It must cover every
   advertised hardware profile, the minimum driver, a real test run ID, and
   all checks enforced by `publish_customer_release.py`.
4. Dispatch `publish-customer-agent.yml` with the release ID, exact digest,
   qualification path, profiles, and minimum driver.
5. The workflow performs an unauthenticated digest pull, validates the report,
   checks the image labels, renders the installer with the signing public key,
   signs the canonical manifest, and creates one GitHub Release containing all
   four files in one command.
6. On a fresh GCP machine, run the command printed in the workflow summary and
   confirm enrollment, workload submission, progress, and the local report.

## Identity chain

The chain is intentionally one-way:

`private source revision -> CI-built image digest -> GCP qualification for the
same digest -> signed release manifest naming the same digest -> customer
installer verifies signature -> Docker verifies the pulled digest`.

Changing the image after qualification invalidates the report. Changing the
manifest after signing invalidates the signature. The installer never accepts
a mutable image tag from the release manifest.

## Files allowed in this repository

- `.github/workflows/publish-customer-agent.yml`
- `.github/workflows/test-release-repository.yml`
- `scripts/publish_customer_release.py`
- `scripts/install_customer_agent.sh`
- `tests/`
- `docs/`
- reviewed qualification JSON files under `qualifications/`
- repository metadata such as this README

Do not copy runtime packages, backend code, evaluation fixtures, model assets,
Docker build contexts, wheelhouses, or signing keys into this repository.
