from __future__ import annotations

import base64
import importlib.util
import json
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/publish-customer-agent.yml"
INSTALLER = ROOT / "scripts/install_customer_agent.sh"
PUBLISHER = ROOT / "scripts/publish_customer_release.py"
CANDIDATE_PUBLISHER = ROOT / "scripts/publish_candidate_acceptance.py"


def load_publisher():
    spec = importlib.util.spec_from_file_location("publish_customer_release", PUBLISHER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_candidate_publisher():
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        spec = importlib.util.spec_from_file_location("publish_candidate_acceptance", CANDIDATE_PUBLISHER)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.pop(0)


class ReleaseRepositoryBoundaryTests(unittest.TestCase):
    def test_publish_checks_exact_uploaded_asset_names_before_release_is_latest(self) -> None:
        source = WORKFLOW.read_text(encoding="utf-8")
        self.assertLess(source.index("gh release upload"),
                        source.index("python scripts/verify_customer_release_assets.py"))
        self.assertLess(source.index("python scripts/verify_customer_release_assets.py"),
                        source.index('gh release edit "$tag"'))

    def test_asset_verifier_fails_on_missing_asset(self) -> None:
        path = ROOT / "scripts" / "verify_customer_release_assets.py"
        spec = importlib.util.spec_from_file_location("verify_customer_release_assets", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        missing = subprocess.CompletedProcess([], 0, "install\nREADME.txt\nrelease.json\n", "")
        complete = subprocess.CompletedProcess([], 0, "install\nREADME.txt\nrelease.json\nrelease.sig\n", "")
        with mock.patch.object(module.subprocess, "run", return_value=missing):
            self.assertFalse(module.verify("sigilantlabs/customer-agent-release", "test", attempts=1))
        with mock.patch.object(module.subprocess, "run", return_value=complete):
            self.assertTrue(module.verify("sigilantlabs/customer-agent-release", "test", attempts=1))

    def test_enrollment_secret_uses_temporary_env_file_not_docker_argv(self) -> None:
        source = INSTALLER.read_text(encoding="utf-8")
        self.assertIn('enrollment_args+=(--env-file "$ENROLLMENT_ENV_FILE")', source)
        self.assertNotIn('enrollment_args+=(--env "SIGILANT_ENROLLMENT_CODE=$ENROLLMENT_CODE")', source)
        self.assertIn('trap cleanup_installer_temp EXIT', source)
        self.assertIn("trap 'exit 130' INT", source)
        self.assertIn("trap 'exit 143' TERM", source)

    def test_bounded_acceptance_alias_matches_customer_handoff(self) -> None:
        result = subprocess.run(
            ["bash", str(INSTALLER), "--enrollment-code", "test-code",
             "--workload", "/tmp/fixture.jsonl", "--credential", "sig_test",
             "--bounded-acceptance", "--sample-cap", "40", "--dry-run"],
            text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("bounded acceptance profile with sample cap 40", result.stdout)

    def test_workflow_has_no_pat_or_registry_secret_dependency(self) -> None:
        source = WORKFLOW.read_text(encoding="utf-8")
        self.assertNotIn("docker/login-action", source)
        self.assertNotIn("SIGILANT_REGISTRY_TOKEN", source)
        self.assertNotIn("PERSONAL_ACCESS_TOKEN", source)
        self.assertNotIn("packages: write", source)
        self.assertIn('visibility="$(gh api', source)

    def test_workflow_accepts_only_the_customer_agent_digest(self) -> None:
        source = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn(r"^ghcr\.io/sigilantlabs/customer-agent@sha256:[0-9a-f]{64}$", source)
        self.assertIn('docker pull "$IMAGE"', source)
        self.assertIn('--qualification "$QUALIFICATION"', source)

    def test_external_actions_are_sha_pinned(self) -> None:
        source = WORKFLOW.read_text(encoding="utf-8")
        uses = re.findall(r"^\s*uses:\s*([^\s#]+)", source, flags=re.MULTILINE)
        self.assertTrue(uses)
        for action in uses:
            self.assertRegex(action, r"^[^@]+@[0-9a-f]{40}$")

    def test_public_urls_are_owned_by_distribution_repository(self) -> None:
        installer = INSTALLER.read_text(encoding="utf-8")
        publisher = PUBLISHER.read_text(encoding="utf-8")
        expected = "github.com/sigilantlabs/customer-agent-release/releases/"
        self.assertIn(expected, installer)
        self.assertIn(expected, publisher)
        self.assertNotIn("sigilant-optimizer/releases/latest/download", installer)
        self.assertNotIn("sigilant-optimizer/releases/latest/download", publisher)

    def test_repository_does_not_contain_product_source_or_private_assets(self) -> None:
        forbidden_roots = {
            "app", "config", "runner", "sigilant_customer_runtime",
            "sigilant_deploy", "wheelhouses", "web",
        }
        present = {path.name for path in ROOT.iterdir()}
        self.assertFalse(forbidden_roots & present)

    def test_publisher_binds_signature_installer_and_exact_digest(self) -> None:
        publisher = load_publisher()
        image = "ghcr.io/sigilantlabs/customer-agent@sha256:" + "a" * 64
        profile = {"model": "NVIDIA L4", "count": 1}
        with tempfile.TemporaryDirectory() as raw_temp:
            temp = Path(raw_temp)
            key = temp / "release.key"
            subprocess.run(
                ["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(key)],
                check=True,
                capture_output=True,
            )
            key.chmod(0o600)
            qualification = temp / "qualification.json"
            qualification.write_text(
                json.dumps({
                    "image": image,
                    "profiles": [profile],
                    "minimum_driver_version": "570.00",
                    "test_run_id": "gcp-l4-release-test",
                    **{name: True for name in publisher.REQUIRED_CHECKS},
                }),
                encoding="utf-8",
            )
            metadata = {
                "Os": "linux",
                "Architecture": "amd64",
                "Config": {"Labels": {"io.sigilant.vllm.version": "0.25.0"}},
            }
            with mock.patch.object(publisher, "_image_metadata", return_value=metadata):
                output = publisher.publish(
                    image=image,
                    control_url="https://optimizer-api-production.up.railway.app",
                    release_id="2026.09.19.1",
                    signing_key=key,
                    signing_key_id="release-1",
                    qualification=qualification,
                    output=temp / "dist",
                    minimum_driver_version="570.00",
                    minimum_docker_version="24.0.0",
                    profiles=[profile],
                    public_base_url=(
                        "https://github.com/sigilantlabs/customer-agent-release/"
                        "releases/download/customer-agent-2026.09.19.1"
                    ),
                )
            manifest = output / "release.json"
            document = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(document["image"], image)
            self.assertEqual(document["qualification"]["test_run_id"], "gcp-l4-release-test")
            installer = (output / "install").read_text(encoding="utf-8")
            self.assertNotIn("__SIGILANT_RELEASE_PUBLIC_KEY_B64__", installer)
            public_key = subprocess.run(
                ["openssl", "pkey", "-in", str(key), "-pubout"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertIn(base64.b64encode(public_key.encode()).decode(), installer)
            public_path = temp / "public.pem"
            public_path.write_text(public_key, encoding="utf-8")
            verification = subprocess.run(
                [
                    "openssl", "pkeyutl", "-verify", "-pubin", "-inkey", str(public_path),
                    "-rawin", "-in", str(manifest), "-sigfile", str(output / "release.sig"),
                ],
                capture_output=True,
            )
            self.assertEqual(verification.returncode, 0, verification.stderr.decode())

    def test_production_assets_are_scope_bound(self) -> None:
        publisher = load_publisher()
        source = INSTALLER.read_text(encoding="utf-8")
        self.assertIn('__SIGILANT_RELEASE_SCOPE__', source)
        self.assertIn('"release_scope": "production"', PUBLISHER.read_text(encoding="utf-8"))
        self.assertIn('release_scope', source)

    def test_installer_waits_for_successful_agent_poll_before_prove(self) -> None:
        source = INSTALLER.read_text(encoding="utf-8")
        self.assertIn('rm -f -- "$STATE_DIR/agent-ready.json"', source)
        function = source.split("wait_for_agent_ready() {", 1)[1].split(
            "\nrun_prove_if_requested() {", 1)[0]
        prove = source.split("run_prove_if_requested() {", 1)[1]
        self.assertLess(prove.index("wait_for_agent_ready"),
                        prove.index('info "starting Prove'))

        with tempfile.TemporaryDirectory() as raw_temp:
            state = Path(raw_temp)
            marker = state / "agent-ready.json"
            marker.write_text('{"agent_protocol":"prove-leg-v2"}', encoding="utf-8")
            script = (
                'set -eu\n'
                f'STATE_DIR={str(state)!r}\n'
                'AGENT_READY_TIMEOUT_SECONDS=1\n'
                'die() { printf "%s\\n" "$*" >&2; exit 2; }\n'
                'info() { :; }\n'
                'docker_cmd() { printf "running\\n"; }\n'
                'wait_for_agent_ready() {' + function + '\nwait_for_agent_ready\n'
            )
            ready = subprocess.run(["bash", "-c", script], text=True, capture_output=True)
            self.assertEqual(ready.returncode, 0, ready.stderr)
            marker.unlink()
            timeout = subprocess.run(["bash", "-c", script], text=True, capture_output=True)
            self.assertEqual(timeout.returncode, 2, timeout.stderr)
            self.assertIn("no Prove run was submitted", timeout.stderr)

    def test_installer_reports_host_package_failures(self) -> None:
        source = INSTALLER.read_text(encoding="utf-8")
        for message in (
            "Docker package index refresh failed",
            "Docker could not be installed automatically",
            "NVIDIA repository verification tools could not be installed",
            "NVIDIA package index refresh failed",
            "NVIDIA container support could not be installed automatically",
        ):
            self.assertIn(message, source)
        checked = subprocess.run(["bash", "-n", str(INSTALLER)], capture_output=True, text=True)
        self.assertEqual(checked.returncode, 0, checked.stderr)

    def test_candidate_workflow_is_pinned_and_cannot_update_latest(self) -> None:
        source = (ROOT / ".github/workflows/publish-candidate-acceptance.yml").read_text(encoding="utf-8")
        self.assertIn("5765d8b6f61e797f1fc52797d0eac8ced317d0b878ea5310c61e40f0f8a622c2", source)
        self.assertIn("--prerelease --latest=false", source)
        self.assertNotIn("qualification", source.lower())

    def test_candidate_publisher_emits_signed_unqualified_candidate(self) -> None:
        candidate = load_candidate_publisher()
        image = "ghcr.io/sigilantlabs/customer-agent@sha256:" + "b" * 64
        with tempfile.TemporaryDirectory() as raw_temp:
            temp = Path(raw_temp)
            key = temp / "release.key"
            subprocess.run(["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(key)],
                           check=True, capture_output=True)
            key.chmod(0o600)
            metadata = {"Os": "linux", "Architecture": "amd64",
                        "Config": {"Labels": {"io.sigilant.vllm.version": "0.25.0"}}}
            with mock.patch.object(candidate, "_image_metadata", return_value=metadata):
                output = candidate.publish_candidate(
                    image=image, control_url="https://app.sigilantlabs.com",
                    candidate_id="candidate-2026.09.20.1", signing_key=key,
                    signing_key_id="candidate-ephemeral-1", output=temp / "dist",
                    minimum_driver_version="580.178.04",
                    public_base_url="https://github.com/sigilantlabs/customer-agent-release/releases/download/customer-agent-candidate-2026.09.20.1")
            document = json.loads((output / "release.json").read_text(encoding="utf-8"))
            self.assertEqual(document["schema"], "sigilant.customer-agent-candidate.v1")
            self.assertEqual(document["candidate_state"], "awaiting-acceptance")
            self.assertNotIn("qualification", document)
            installer = (output / "install").read_text(encoding="utf-8")
            self.assertIn('EXPECTED_RELEASE_SCHEMA="sigilant.customer-agent-candidate.v1"', installer)
            self.assertIn('DEFAULT_CONTROL_URL=https://app.sigilantlabs.com', installer)
            self.assertNotIn("__SIGILANT_RELEASE_SCHEMA__", installer)
            public_key = subprocess.run(["openssl", "pkey", "-in", str(key), "-pubout"],
                                        check=True, capture_output=True, text=True).stdout
            public_path = temp / "public.pem"
            public_path.write_text(public_key, encoding="utf-8")
            verify = ["openssl", "pkeyutl", "-verify", "-pubin", "-inkey", str(public_path),
                      "-rawin", "-in", str(output / "release.json"),
                      "-sigfile", str(output / "release.sig")]
            self.assertEqual(subprocess.run(verify, capture_output=True).returncode, 0)
            (output / "release.json").write_text("{}\n", encoding="utf-8")
            self.assertNotEqual(subprocess.run(verify, capture_output=True).returncode, 0)


if __name__ == "__main__":
    unittest.main()
