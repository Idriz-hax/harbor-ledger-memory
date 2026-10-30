#!/usr/bin/env python3
"""Temporarily patch npm's bundles until npm/cli #10170 is in a release."""
import base64
import gzip
import hashlib
import json
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from registry_download import REGISTRY_OPENER, package_tarball_url

ROOT = Path(__file__).resolve().parents[1]
PINS = {
    "npm": ("12.2.1", "bKCmFRVT/BB+YPEYbZwQ1XUI3N5t4xKSpU1MxOjo7z3iWzKBJ0ZXRC1xFdEN+TC7N5TdywaBzf2dMXGJq/e2PQ=="),
    "undici": ("6.29.1", "EvRlUt/QqAEvXdQeRMYUfwOQsfzpDztykrG2owGEE/rBOBoYv9uF39ZzsNDh4lTFHhTr2XOZjKkYgueD12ZmGA=="),
    "ip-address": ("10.7.5", "t2v0rkILYv9WXmSL7TpAaU3HXgaFYOPUcH3QHgwQ++gpJuH3bqyMy4Fvt/DJCTKwzH3Cf+bAp5W/mFL39KPLzA=="),
}
ARTIFACT = ROOT / "vendor" / "npm-12.2.1-undici-6.29.1-ip-address-10.7.5.tgz"


def verify(archive, integrity, name):
    actual = base64.b64encode(hashlib.sha512(archive.read_bytes()).digest()).decode()
    if actual != integrity:
        raise ValueError(f"SHA-512 integrity mismatch for {name}")


def download_registry(name, version, integrity, archive):
    metadata_url = f"https://registry.npmjs.org/{name}/{version}"
    with REGISTRY_OPENER.open(metadata_url) as response:
        metadata = json.load(response)
    if metadata.get("name") != name or metadata.get("version") != version:
        raise ValueError(f"unexpected registry metadata for {name}@{version}")
    with REGISTRY_OPENER.open(package_tarball_url(name, version)) as response:
        archive.write_bytes(response.read())
    verify(archive, integrity, f"{name}@{version}")


def extract_safe(archive, destination):
    with tarfile.open(archive, "r:gz") as source:
        for member in source.getmembers():
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or not (member.isfile() or member.isdir()):
                raise ValueError(f"unsafe archive member: {member.name}")
        source.extractall(destination, filter="data")


with tempfile.TemporaryDirectory() as temp:
    work = Path(temp)
    # npm pack downloads the official source archive; output is verified against its pinned SRI.
    subprocess.run(["npm", "pack", f"npm@{PINS['npm'][0]}", "--pack-destination", str(work)], check=True, cwd=work, stdout=subprocess.DEVNULL)
    original = work / "npm-12.2.1.tgz"
    verify(original, PINS["npm"][1], "npm@12.2.1")
    extract_safe(original, work / "npm")
    package = work / "npm" / "package"
    for name in ("undici", "ip-address"):
        version, integrity = PINS[name]
        archive = work / f"{name}.tgz"
        download_registry(name, version, integrity, archive)
        extracted = work / name
        extract_safe(archive, extracted)
        target = package / "node_modules" / name
        replacement = extracted / "package"
        if not replacement.is_dir():
            raise ValueError(f"missing package/ in {name} archive")
        shutil.rmtree(target)
        shutil.copytree(replacement, target)
        manifest = json.loads((target / "package.json").read_text())
        if manifest.get("name") != name or manifest.get("version") != version:
            raise ValueError(f"replacement manifest mismatch for {name}")

    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    def normalize(member):
        member.mtime = 0
        member.uid = member.gid = 0
        member.uname = member.gname = ""
        return member

    with ARTIFACT.open("wb") as raw:
        with gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as output:
                output.add(package, arcname="package", recursive=True, filter=normalize)

# Verify archive identity, patched dependencies, and key CLI entry points before publishing it.
with tarfile.open(ARTIFACT, "r:gz") as result:
    required = {"package/package.json", "package/bin/npm", "package/bin/npm-cli.js",
                "package/node_modules/undici/package.json", "package/node_modules/ip-address/package.json"}
    names = set(result.getnames())
    if not required <= names:
        raise ValueError(f"artifact missing required paths: {sorted(required - names)}")
    for dep, version in (("undici", "6.29.1"), ("ip-address", "10.7.5")):
        manifest = json.load(result.extractfile(f"package/node_modules/{dep}/package.json"))
        if manifest.get("version") != version:
            raise ValueError(f"artifact has unexpected {dep} version")
    npm = json.load(result.extractfile("package/package.json"))
    if (npm.get("name"), npm.get("version")) != ("npm", "12.2.1"):
        raise ValueError("artifact identity mismatch")
print(f"Built {ARTIFACT.name}; sha512-{base64.b64encode(hashlib.sha512(ARTIFACT.read_bytes()).digest()).decode()}")
