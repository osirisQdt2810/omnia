#!/usr/bin/env python3
"""Vendor ``requirements/requirements-vendor.txt`` into ``src/omnia/vendor`` (per-OS layout).

Anki does not pip-install for users, so runtime deps are bundled with the add-on and added
to ``sys.path`` at startup. Most deps are pure-Python and go in ``vendor/universal/``. The
one exception is ``pydantic_core`` — a compiled (Rust) wheel — which differs per OS/arch and
**cannot** share a folder across macOS arm64 vs x86_64 (same ``…-darwin.so`` filename). So
each platform gets its own ``vendor/<os_arch>/pydantic_core/`` and the startup loader in
``omnia/__init__.py`` adds only the matching one. Shipping all of them keeps a single
cross-platform ``.ankiaddon``.

Usage:
    python scripts/vendor_deps.py                 # cp313, all 4 platforms (Anki 25.09+/26.x)
    python scripts/vendor_deps.py --abi cp312      # a different interpreter ABI
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VENDOR_DIR = REPO_ROOT / "src" / "omnia" / "vendor"
UNIVERSAL_DIR = VENDOR_DIR / "universal"
REQ_FILE = REPO_ROOT / "requirements" / "requirements-vendor.txt"

# Packages that ship a compiled binary → vendored per-platform, not in universal/.
BINARY_PACKAGES = ("pydantic_core",)

# (vendor subdir, pip --platform tag). Anki ships 64-bit CPython on every desktop OS.
PLATFORMS = {
    "mac_arm64": "macosx_11_0_arm64",
    "mac_x64": "macosx_10_12_x86_64",
    "win_x64": "win_amd64",
    "linux_x64": "manylinux2014_x86_64",
}


def _has_real_requirements() -> bool:
    if not REQ_FILE.exists():
        return False
    return any(
        line.strip() and not line.strip().startswith("#")
        for line in REQ_FILE.read_text(encoding="utf-8").splitlines()
    )


def _pip(*args: str) -> None:
    cmd = [sys.executable, "-m", "pip", *args]
    print(" ".join(cmd))
    subprocess.run(cmd, check=True)


def _installed_version(package: str) -> str:
    """Read a package's version from its dist-info in ``universal/`` (e.g. pydantic_core)."""
    for info in UNIVERSAL_DIR.glob(f"{package}-*.dist-info"):
        return info.name.removesuffix(".dist-info").split("-")[-1]
    raise SystemExit(f"Could not determine vendored {package} version from {UNIVERSAL_DIR}")


def _strip_native_from_universal() -> None:
    """Remove any compiled artifacts from universal/ (must be pure-Python + cross-platform)."""
    for pattern in ("*.so", "*.pyd"):
        for binary in UNIVERSAL_DIR.rglob(pattern):
            binary.unlink()
    for package in BINARY_PACKAGES:
        shutil.rmtree(UNIVERSAL_DIR / package, ignore_errors=True)
        for info in UNIVERSAL_DIR.glob(f"{package}-*.dist-info"):
            shutil.rmtree(info, ignore_errors=True)


def _vendor_binary_per_platform(package: str, version: str, abi: str) -> None:
    """Download ``package``'s wheel for each target platform and extract its package dir."""
    for subdir, plat_tag in PLATFORMS.items():
        dest = VENDOR_DIR / subdir
        shutil.rmtree(dest / package, ignore_errors=True)
        dest.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as tmp:
            _pip(
                "download", "--no-deps", "--only-binary=:all:",
                "--python-version", abi.removeprefix("cp"),
                "--platform", plat_tag,
                "-d", tmp, f"{package}=={version}",
            )
            wheel = next(Path(tmp).glob(f"{package}-*.whl"), None)
            if wheel is None:
                raise SystemExit(f"No {package} wheel for {plat_tag} (abi {abi})")
            extract = Path(tmp) / "x"
            with zipfile.ZipFile(wheel) as zf:
                zf.extractall(extract)
            shutil.copytree(extract / package, dest / package)
            print(f"  {subdir} <- {wheel.name}")


def vendor(abi: str) -> None:
    if not _has_real_requirements():
        print("requirements-vendor.txt has no packages — nothing to vendor.")
        return

    UNIVERSAL_DIR.mkdir(parents=True, exist_ok=True)
    _pip(
        "install", "--no-compile", "--upgrade",
        "--target", str(UNIVERSAL_DIR), "-r", str(REQ_FILE),
    )
    versions = {pkg: _installed_version(pkg) for pkg in BINARY_PACKAGES}
    _strip_native_from_universal()
    for package, version in versions.items():
        print(f"Vendoring {package}=={version} per platform (abi {abi})…")
        _vendor_binary_per_platform(package, version, abi)
    print(f"Vendored into {VENDOR_DIR} (universal + {', '.join(PLATFORMS)})")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--abi",
        default="cp313",
        help="CPython ABI tag of the target Anki interpreter (default cp313 = Anki 25.09+).",
    )
    vendor(parser.parse_args().abi)
