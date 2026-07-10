"""TLS verification helpers for direct outbound provider calls."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from functools import lru_cache


@lru_cache(maxsize=4)
def macos_direct_ca_bundle(base_bundle: str | None) -> str | None:
    if sys.platform != "darwin":
        return base_bundle

    # curl succeeds on macOS because it can trust operator-installed keychain
    # roots that Python/httpx does not see when we bypass env-driven defaults.
    keychain_export = subprocess.run(
        [
            "security",
            "find-certificate",
            "-a",
            "-p",
            "/Library/Keychains/System.keychain",
            "/System/Library/Keychains/SystemRootCertificates.keychain",
            os.path.expanduser("~/Library/Keychains/login.keychain-db"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    keychain_pem = keychain_export.stdout.strip()
    if not keychain_pem:
        return base_bundle

    with tempfile.NamedTemporaryFile(mode="w", prefix="agency-direct-ca-", suffix=".pem", delete=False) as handle:
        if base_bundle and os.path.exists(base_bundle):
            with open(base_bundle, "r", encoding="utf-8") as source:
                handle.write(source.read())
            handle.write("\n")
        handle.write(keychain_pem)
        handle.write("\n")
        return handle.name


def direct_tls_verify() -> str | bool | None:
    """Return an explicit CA bundle for direct calls that intentionally ignore proxy env."""
    for name in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        value = os.environ.get(name)
        if value and os.path.exists(value):
            return macos_direct_ca_bundle(value)
    return macos_direct_ca_bundle(None)
