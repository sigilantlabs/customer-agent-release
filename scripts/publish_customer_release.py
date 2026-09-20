#!/usr/bin/env python3
"""Create the signed files served by the one-command customer installer.

This command prepares a release; it does not publish anything and it never
accepts a private key from the repository or from a Docker build argument. The
CI workflow supplies the key through a protected file at release time. The
resulting directory can be uploaded as-is to the configured installer origin:

    install       (rendered installer with the public key baked in)
    release.json  (canonical manifest)
    release.sig   (Ed25519 signature over release.json)

The image must already be pushed and addressed by an immutable registry digest.
Qualification is required for the exact image and every hardware profile the
manifest advertises.
"""
from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess
from urllib.parse import urlsplit


IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9./_-]+@[A-Za-z0-9_-]+:[a-f0-9]{64}$")
VERSION_RE = re.compile(r"^\d+(?:\.\d+){1,3}$")
ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
REQUIRED_CHECKS = (
    "real_model_inference",
    "account_job_receipt_roundtrip",
    "cancel_stops_gpu_processes",
    "egress_review",
    "private_assets_absent",
    "final_recommendation_verified",
)

# GitHub Releases is the default public registry.  It is intentionally a
# public, immutable release asset origin: the signed manifest authenticates
# the image and the installer, while GitHub supplies the download service.
# A production deployment may pass its branded HTTPS mirror with
# ``--public-base-url`` without changing the signed format.
DEFAULT_PUBLIC_BASE_URL = (
    "https://github.com/sigilantlabs/customer-agent-release/releases/latest/download"
)


def canonical_json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode()


def _run(command: list[str]) -> str:
    try:
        return subprocess.run(command, check=True, capture_output=True, text=True).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError("release command failed: " + " ".join(command[:3])) from exc


def _public_key(private_key: Path, key_id: str) -> str:
    if not ID_RE.fullmatch(key_id):
        raise ValueError("release signing key ID is invalid")
    if not private_key.is_file() or private_key.is_symlink() or private_key.stat().st_mode & 0o077:
        raise ValueError("an Ed25519 private signing key with mode 0600 is required")
    detail = _run(["openssl", "pkey", "-in", str(private_key), "-text", "-noout"])
    public = _run(["openssl", "pkey", "-in", str(private_key), "-pubout"])
    if "ED25519" not in detail.upper() or "BEGIN PUBLIC KEY" not in public:
        raise ValueError("the release signing key must be Ed25519")
    return public


def _public_urls(base_url: str) -> dict[str, str]:
    parsed = urlsplit(base_url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment
            or any(c in base_url for c in "\r\n")):
        raise ValueError("public release URL must be a credential-free HTTPS URL")
    base = base_url.rstrip("/")
    return {
        "installer": f"{base}/install",
        "manifest": f"{base}/release.json",
        "signature": f"{base}/release.sig",
    }


def _image_metadata(image: str) -> dict:
    if not IMAGE_RE.fullmatch(image):
        raise ValueError("image must be an immutable registry reference ending in @sha256:<64 hex chars>")
    try:
        inspected = json.loads(_run(["docker", "image", "inspect", image]))[0]
    except (IndexError, json.JSONDecodeError, TypeError) as exc:
        raise ValueError("docker could not inspect the release image") from exc
    if inspected.get("Os") != "linux" or inspected.get("Architecture") != "amd64":
        raise ValueError("release image must target Linux amd64")
    labels = inspected.get("Config", {}).get("Labels", {})
    if not isinstance(labels, dict) or labels.get("io.sigilant.vllm.version") != "0.25.0":
        raise ValueError("release image must carry the approved vLLM 0.25.0 label")
    return inspected


def _qualification(record_path: Path, image: str, profiles: list[dict], minimum_driver: str) -> dict:
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("qualification report is invalid") from exc
    if not isinstance(record, dict):
        raise ValueError("qualification report is invalid")
    image_ok = record.get("image") == image or record.get("image_ref") == image
    # Legacy qualification reports identify the local config digest. New
    # reports should carry the exact registry reference above.
    if not image_ok:
        raise ValueError("qualification must cover this exact immutable image")
    qualified = record.get("profiles")
    if not isinstance(qualified, list):
        raise ValueError("qualification must list every qualified hardware profile")
    for profile in profiles:
        if not any(isinstance(item, dict) and item == profile for item in qualified):
            raise ValueError("qualification is missing an advertised hardware profile")
    if record.get("minimum_driver_version") != minimum_driver:
        raise ValueError("qualification must cover the released minimum driver")
    if any(record.get(name) is not True for name in REQUIRED_CHECKS):
        raise ValueError("qualification must pass all customer release checks")
    if not isinstance(record.get("test_run_id"), str) or not record["test_run_id"].strip():
        raise ValueError("qualification must include a test run ID")
    return record


def render_installer(destination: Path, public_key: str, manifest_url: str,
                     release_scope: str = "production",
                     release_schema: str = "sigilant.customer-agent-release.v1",
                     control_url: str = "https://optimizer-api-production.up.railway.app") -> None:
    source = Path(__file__).with_name("install_customer_agent.sh").read_text(encoding="utf-8")
    encoded = base64.b64encode(public_key.encode()).decode("ascii")
    rendered = source.replace("__SIGILANT_RELEASE_PUBLIC_KEY_B64__", encoded)
    rendered = rendered.replace(
        "https://github.com/sigilantlabs/customer-agent-release/releases/latest/download/release.json",
        manifest_url,
    )
    rendered = rendered.replace("__SIGILANT_RELEASE_SCOPE__", release_scope)
    rendered = rendered.replace("__SIGILANT_RELEASE_SCHEMA__", release_schema)
    rendered = rendered.replace(
        'readonly DEFAULT_CONTROL_URL="https://optimizer-api-production.up.railway.app"',
        f"readonly DEFAULT_CONTROL_URL={shlex.quote(control_url.rstrip('/'))}",
    )
    if "__SIGILANT_RELEASE_PUBLIC_KEY_B64__" in rendered:
        raise ValueError("installer signing placeholder was not replaced")
    destination.write_text(rendered, encoding="utf-8")
    destination.chmod(0o755)


def publish(*, image: str, control_url: str, release_id: str, signing_key: Path,
            signing_key_id: str, qualification: Path, output: Path,
            minimum_driver_version: str, minimum_docker_version: str,
            profiles: list[dict], public_base_url: str = DEFAULT_PUBLIC_BASE_URL) -> Path:
    parsed = urlsplit(control_url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or any(c in control_url for c in "\r\n")):
        raise ValueError("control URL must be a credential-free HTTPS origin")
    if not ID_RE.fullmatch(release_id):
        raise ValueError("release ID is invalid")
    if not VERSION_RE.fullmatch(minimum_driver_version) or not VERSION_RE.fullmatch(minimum_docker_version):
        raise ValueError("minimum versions must be exact dotted versions")
    if not profiles or any(set(profile) != {"model", "count"} or not isinstance(profile["model"], str)
                           or type(profile["count"]) is not int or profile["count"] < 1 for profile in profiles):
        raise ValueError("at least one valid hardware profile is required")
    # Preserve advertised order while rejecting duplicate profiles.
    if len({(p["model"], p["count"]) for p in profiles}) != len(profiles):
        raise ValueError("hardware profiles must be unique")
    public_urls = _public_urls(public_base_url)
    inspected = _image_metadata(image)
    record = _qualification(qualification, image, profiles, minimum_driver_version)
    public = _public_key(signing_key, signing_key_id)
    if output.exists():
        raise ValueError("release output already exists; choose a new directory")
    output.mkdir(parents=True)
    try:
        manifest = {
            "schema": "sigilant.customer-agent-release.v1",
            "release_scope": "production",
            "release_id": release_id,
            "signer_key_id": signing_key_id,
            "image": image,
            "control_url": control_url.rstrip("/"),
            "runtime": {"vllm": inspected["Config"]["Labels"]["io.sigilant.vllm.version"]},
            "hardware_profiles": profiles,
            "minimum_driver_version": minimum_driver_version,
            "minimum_docker_version": minimum_docker_version,
            "public_urls": public_urls,
            "qualification": {"test_run_id": record["test_run_id"], "checks": list(REQUIRED_CHECKS)},
        }
        manifest_path = output / "release.json"
        manifest_path.write_bytes(canonical_json(manifest))
        _run(["openssl", "pkeyutl", "-sign", "-inkey", str(signing_key), "-rawin",
              "-in", str(manifest_path), "-out", str(output / "release.sig")])
        render_installer(output / "install", public, public_urls["manifest"], "production",
                         control_url=control_url)
        (output / "README.txt").write_text(
            "Sigilant customer agent release " + release_id + "\n\n"
            "Serve install, release.json, and release.sig from the configured HTTPS origin.\n"
            "Customers run:\n"
            "  curl -fsSL " + public_urls["installer"] +
            " | bash -s -- --enrollment-code YOUR_CODE\n",
            encoding="utf-8")
    except Exception:
        shutil.rmtree(output, ignore_errors=True)
        raise
    return output


def _profile(value: str) -> dict:
    try:
        model, count = value.rsplit(":", 1)
        profile = {"model": model, "count": int(count)}
    except (ValueError, TypeError):
        raise argparse.ArgumentTypeError("hardware profile must be MODEL:COUNT") from None
    return profile


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--control-url", required=True)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--signing-key", required=True, type=Path)
    parser.add_argument("--signing-key-id", required=True)
    parser.add_argument("--qualification", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--minimum-driver-version", required=True)
    parser.add_argument("--minimum-docker-version", default="24.0.0")
    parser.add_argument("--public-base-url", default=DEFAULT_PUBLIC_BASE_URL,
                        help="HTTPS directory serving install, release.json and release.sig")
    parser.add_argument("--hardware-profile", action="append", type=_profile, required=True,
                        help="qualified GPU profile as MODEL:COUNT; may be repeated")
    args = parser.parse_args()
    try:
        print(publish(image=args.image, control_url=args.control_url, release_id=args.release_id,
                      signing_key=args.signing_key, signing_key_id=args.signing_key_id,
                      qualification=args.qualification, output=args.output,
                      minimum_driver_version=args.minimum_driver_version,
                      minimum_docker_version=args.minimum_docker_version,
                      profiles=args.hardware_profile, public_base_url=args.public_base_url))
    except ValueError as error:
        parser.exit(2, str(error) + "\n")
