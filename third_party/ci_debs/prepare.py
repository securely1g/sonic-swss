#!/usr/bin/env python3
"""Prepare the pinned production CI DEBs for the local Bazel importer."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any
import urllib.error
import urllib.parse
import urllib.request
import zipfile


PACKAGES = (
    "libdashapi",
    "libsaimetadata",
    "libsaimetadata-dev",
    "libsairedis",
    "libsairedis-dev",
    "libsaivs-dev",
    "libswsscommon-dev",
)
CHUNK_SIZE = 1024 * 1024


class PreparationError(Exception):
    pass


class HttpsRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        if urllib.parse.urlsplit(new_url).scheme != "https":
            raise PreparationError("artifact download redirected to a non-HTTPS URL")
        return super().redirect_request(request, response, code, message, headers, new_url)


def load_manifest(architecture: str) -> tuple[bytes, dict[str, Any]]:
    path = Path(__file__).with_name(f"manifest.{architecture}.json")
    try:
        contents = path.read_bytes()
        manifest = json.loads(contents)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PreparationError(f"cannot read pinned manifest: {path}") from error
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise PreparationError("pinned manifest must use schema_version 1")
    if manifest.get("distribution") != "trixie" or manifest.get("architecture") != architecture:
        raise PreparationError("pinned manifest distribution or architecture does not match")
    packages = manifest.get("packages")
    provenance = manifest.get("provenance")
    if not isinstance(packages, dict) or set(packages) != set(PACKAGES):
        raise PreparationError("pinned manifest must contain exactly the seven production packages")
    if not isinstance(provenance, dict):
        raise PreparationError("pinned manifest has no artifact provenance")
    for name in PACKAGES:
        package = packages[name]
        basename = f"{name}_1.0.0_{architecture}.deb"
        if not isinstance(package, dict) or package.get("path") != basename:
            raise PreparationError(f"invalid pinned package basename for {name}")
        if not isinstance(package.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", package["sha256"]):
            raise PreparationError(f"invalid pinned SHA-256 for {name}")
        artifact_key = package.get("artifact")
        artifact = provenance.get(artifact_key) if isinstance(artifact_key, str) else None
        if not isinstance(artifact, dict) or not isinstance(artifact.get("artifact"), str):
            raise PreparationError(f"invalid artifact reference for {name}")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", artifact["artifact"]):
            raise PreparationError(f"invalid artifact name for {name}")
        if package.get("member") != f"{artifact['artifact']}/{basename}":
            raise PreparationError(f"invalid pinned ZIP member for {name}")
        url = artifact.get("download_url")
        if not isinstance(url, str):
            raise PreparationError(f"missing HTTPS artifact URL for {name}")
        try:
            parsed = urllib.parse.urlsplit(url)
        except ValueError as error:
            raise PreparationError(f"invalid HTTPS artifact URL for {name}") from error
        if parsed.scheme != "https" or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.fragment:
            raise PreparationError(f"invalid HTTPS artifact URL for {name}")
    return contents, manifest


def require_regular_or_missing(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    except OSError as error:
        raise PreparationError(f"cannot inspect output path: {path}") from error
    if not stat.S_ISREG(mode):
        raise PreparationError(f"output path must be a regular file or absent: {path}")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(CHUNK_SIZE):
                digest.update(chunk)
    except OSError as error:
        raise PreparationError(f"cannot read existing output: {path}") from error
    return digest.hexdigest()


def download_archive(artifact: dict[str, Any], destination: Path) -> None:
    opener = urllib.request.build_opener(HttpsRedirectHandler())
    request = urllib.request.Request(artifact["download_url"], headers={"User-Agent": "sonic-swss-ci-debs/1"})
    try:
        with opener.open(request, timeout=60) as response, destination.open("xb") as output:
            while chunk := response.read(CHUNK_SIZE):
                output.write(chunk)
    except PreparationError:
        raise
    except urllib.error.HTTPError as error:
        raise PreparationError(f"download failed for {artifact['artifact']}: HTTP {error.code}") from error
    except Exception as error:
        # Redirect URLs can contain temporary credentials. Keep them out of logs.
        raise PreparationError(f"download failed for {artifact['artifact']}") from error


def publish_package(archive: zipfile.ZipFile, package: dict[str, Any], temporary: Path, destination: Path) -> None:
    matches = [info for info in archive.infolist() if info.filename == package["member"]]
    if len(matches) != 1 or matches[0].is_dir():
        raise PreparationError(f"ZIP must contain exactly one regular member for {package['path']}")
    info = matches[0]
    file_type = stat.S_IFMT(info.external_attr >> 16)
    if file_type not in (0, stat.S_IFREG) or info.flag_bits & 1:
        raise PreparationError(f"ZIP member is not an unencrypted regular file: {package['path']}")
    digest = hashlib.sha256()
    try:
        with archive.open(info) as source, temporary.open("xb") as output:
            while chunk := source.read(CHUNK_SIZE):
                digest.update(chunk)
                output.write(chunk)
    except (OSError, EOFError, RuntimeError, zipfile.BadZipFile) as error:
        raise PreparationError(f"cannot read pinned ZIP member for {package['path']}") from error
    if digest.hexdigest() != package["sha256"]:
        raise PreparationError(f"SHA-256 mismatch for {package['path']}")
    require_regular_or_missing(destination)
    try:
        os.replace(temporary, destination)
    except OSError as error:
        raise PreparationError(f"cannot publish output: {destination}") from error


def prepare(architecture: str, output_directory: Path) -> tuple[int, int]:
    contents, manifest = load_manifest(architecture)
    output_directory = output_directory.absolute()
    try:
        output_directory.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise PreparationError(f"cannot create output directory: {output_directory}") from error
    manifest_output = output_directory / "manifest.json"
    require_regular_or_missing(manifest_output)
    pending: dict[str, list[dict[str, Any]]] = {}
    reused = 0
    for name in PACKAGES:
        package = manifest["packages"][name]
        destination = output_directory / package["path"]
        require_regular_or_missing(destination)
        if destination.is_file() and file_sha256(destination) == package["sha256"]:
            reused += 1
        else:
            pending.setdefault(package["artifact"], []).append(package)
    with tempfile.TemporaryDirectory(prefix=".sonic-ci-debs-", dir=output_directory) as directory:
        work = Path(directory)
        for index, (artifact_key, packages) in enumerate(pending.items()):
            artifact = manifest["provenance"][artifact_key]
            archive_path = work / f"artifact-{index}.zip"
            print(f"Downloading {artifact['artifact']}")
            download_archive(artifact, archive_path)
            try:
                with zipfile.ZipFile(archive_path) as archive:
                    for package in packages:
                        publish_package(archive, package, work / package["path"], output_directory / package["path"])
            except zipfile.BadZipFile as error:
                raise PreparationError(f"invalid ZIP for {artifact['artifact']}") from error
        try:
            unchanged = manifest_output.is_file() and manifest_output.read_bytes() == contents
        except OSError as error:
            raise PreparationError(f"cannot read existing output: {manifest_output}") from error
        if not unchanged:
            temporary = work / "manifest.json"
            try:
                temporary.write_bytes(contents)
                require_regular_or_missing(manifest_output)
                os.replace(temporary, manifest_output)
            except OSError as error:
                raise PreparationError(f"cannot publish output: {manifest_output}") from error
    return reused, len(PACKAGES) - reused


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--architecture", choices=("amd64", "arm64"), required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    args = parser.parse_args()
    try:
        reused, downloaded = prepare(args.architecture, args.output_directory)
    except PreparationError as error:
        parser.exit(1, f"error: {error}\n")
    print(f"Prepared {len(PACKAGES)} {args.architecture} packages ({reused} reused, {downloaded} downloaded): {args.output_directory.absolute() / 'manifest.json'}")


if __name__ == "__main__":
    main()
