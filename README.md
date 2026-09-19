# customer-agent-release

This repository exists solely to run the `Build and publish customer agent
release` GitHub Actions workflow (`.github/workflows/publish-customer-agent.yml`).
It signs and publishes the public installer, manifest, and signature for the
Sigilant customer GPU agent as GitHub Release assets, given an already-built
and qualified immutable image digest.

It is deliberately minimal and contains no Sigilant product source code,
backend logic, golden evaluation sets, or business documents — only the
workflow and the two small scripts it invokes
(`scripts/publish_customer_release.py`, `scripts/install_customer_agent.sh`).
Nothing here should ever need to change except in lockstep with that workflow.

This repository must be public before the first customer release. GitHub
Release assets in a private repository are not anonymously downloadable. The
workflow checks repository visibility and stops before signing or creating a
release when this condition is not met.

The container image is built in the private `sigilant-optimizer` repository.
That build uses only its repository-scoped `GITHUB_TOKEN`. After the GHCR
`sigilantlabs/customer-agent` package is deliberately made public, this
repository pulls the qualified immutable digest without logging in to GHCR.
No classic PAT, fine-grained PAT, registry secret, or cross-repository package
credential belongs in this repository.

Before dispatching a real release, a real qualification report for the exact
image digest being released must be committed to this repository at the path
given as the `qualification` workflow input — see that script's docstring and
`REQUIRED_CHECKS` for what it must contain. No such report is checked in here
by default.

Before the first dispatch, configure only these release secrets and variable:

- `SIGILANT_RELEASE_PRIVATE_KEY`: the Ed25519 private signing key.
- `SIGILANT_RELEASE_KEY_ID`: the matching public-key identifier.
- `SIGILANT_CONTROL_URL`: repository variable for the production HTTPS control
  plane.

The workflow accepts only
`ghcr.io/sigilantlabs/customer-agent@sha256:<64 hex>` and requires the
qualification report to name that exact reference. See
`docs/release-runbook.md` for the complete handoff and launch gates.
