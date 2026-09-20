#!/usr/bin/env python3
"""Build signed assets for a nonproduction candidate acceptance prerelease."""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil
from urllib.parse import urlsplit

from publish_customer_release import (
    ID_RE, VERSION_RE, _image_metadata, _public_key, _public_urls, _run,
    canonical_json, render_installer,
)

def publish_candidate(*, image: str, control_url: str, candidate_id: str,
                      signing_key: Path, signing_key_id: str, output: Path,
                      minimum_driver_version: str,
                      public_base_url: str) -> Path:
    parsed = urlsplit(control_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("control URL must be a credential-free HTTPS origin")
    if not ID_RE.fullmatch(candidate_id) or not candidate_id.startswith("candidate-"):
        raise ValueError("candidate ID must start with candidate-")
    if not VERSION_RE.fullmatch(minimum_driver_version):
        raise ValueError("minimum driver version must be an exact dotted version")
    urls = _public_urls(public_base_url)
    inspected = _image_metadata(image)
    public = _public_key(signing_key, signing_key_id)
    if output.exists():
        raise ValueError("candidate output already exists; choose a new directory")
    output.mkdir(parents=True)
    try:
        manifest = {
            "schema": "sigilant.customer-agent-candidate.v1",
            "release_scope": "candidate-acceptance",
            "release_id": candidate_id,
            "signer_key_id": signing_key_id,
            "image": image,
            "control_url": control_url.rstrip("/"),
            "runtime": {"vllm": inspected["Config"]["Labels"]["io.sigilant.vllm.version"]},
            "minimum_driver_version": minimum_driver_version,
            "public_urls": urls,
            "candidate_state": "awaiting-acceptance",
        }
        manifest_path = output / "release.json"
        manifest_path.write_bytes(canonical_json(manifest))
        _run(["openssl", "pkeyutl", "-sign", "-inkey", str(signing_key), "-rawin",
              "-in", str(manifest_path), "-out", str(output / "release.sig")])
        render_installer(output / "install", public, urls["manifest"], "candidate-acceptance",
                         "sigilant.customer-agent-candidate.v1", control_url)
        (output / "README.txt").write_text(
            f"Sigilant NONPRODUCTION candidate acceptance {candidate_id}\n\n"
            "This prerelease is for acceptance testing only. It is not qualified for production.\n",
            encoding="utf-8")
    except Exception:
        shutil.rmtree(output, ignore_errors=True)
        raise
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("image", "control-url", "candidate-id", "signing-key-id",
                 "minimum-driver-version", "public-base-url"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--signing-key", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        print(publish_candidate(**vars(args)))
    except ValueError as error:
        parser.exit(2, str(error) + "\n")
