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

Before dispatching a real release, a real qualification report for the exact
image digest being released must be committed to this repository at the path
given as the `qualification` workflow input — see that script's docstring and
`REQUIRED_CHECKS` for what it must contain. No such report is checked in here
by default.
