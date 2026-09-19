from __future__ import annotations

import base64
import importlib.util
import json
import re
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/publish-customer-agent.yml"
INSTALLER = ROOT / "scripts/install_customer_agent.sh"
PUBLISHER = ROOT / "scripts/publish_customer_release.py"


def load_publisher():
    spec = importlib.util.spec_from_file_location("publish_customer_release", PUBLISHER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReleaseRepositoryBoundaryTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
