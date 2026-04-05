#!/usr/bin/env python3

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

MARKETPLACE_URL = os.getenv(
    "VSCODE_MARKETPLACE_URL",
    "https://marketplace.visualstudio.com/_apis/public/gallery/extensionquery",
)
MARKETPLACE_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json;api-version=7.1-preview.1",
}
PRERELEASE_TOKEN_RE = re.compile(r"(?i)(^|[.-])(alpha|beta|rc|pre|preview)([.-]|$)")
PRERELEASE_SUFFIX_RE = re.compile(r"-[0-9A-Za-z]")
DEFAULT_JOBS = min(8, max(1, os.cpu_count() or 4))
DEFAULT_SHA256_KEY = "default"

NIX_SYSTEM_TO_TARGET_PLATFORM = {
    "x86_64-linux": "linux-x64",
    "aarch64-linux": "linux-arm64",
    "armv7l-linux": "linux-armhf",
    "x86_64-darwin": "darwin",
    "aarch64-darwin": "darwin-arm64",
}

HashValue = str | dict[str, str]


class UpdateError(RuntimeError):
    """Raised when the lock file cannot be processed safely."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Update pinned VS Code Marketplace extensions in a JSON lock file. "
            "The file may be either a flat list or an attribute set of groups."
        )
    )
    parser.add_argument(
        "path",
        help="Path to the Marketplace extension lock file to read and update.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Print pending updates without writing the file. Exits with code 1 when updates are available.",
    )
    parser.add_argument(
        "--include-prerelease",
        action="store_true",
        help="Allow prerelease versions for every extension unless an entry sets prerelease = false.",
    )
    parser.add_argument(
        "--group",
        action="append",
        default=[],
        metavar="NAME",
        help="Limit updates to one or more groups in a grouped lock file. Repeat the flag to select multiple groups.",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=DEFAULT_JOBS,
        metavar="N",
        help=f"Maximum number of concurrent update jobs. Default: {DEFAULT_JOBS}.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Recompute hashes for all entries regardless of whether the version changed.",
    )
    return parser.parse_args()


def read_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as err:
        raise UpdateError(f"Lock file not found: {path}") from err
    except json.JSONDecodeError as err:
        raise UpdateError(f"Lock file is not valid JSON: {path}: {err}") from err


def read_json_with_retries(request: urllib.request.Request, attempts: int = 3) -> dict[str, Any]:
    last_error: Exception | None = None
    for _ in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as err:
            last_error = err
    if last_error is not None:
        raise last_error
    raise UpdateError("Marketplace request failed without returning an error.")


def is_prerelease(version_info: dict[str, Any], version: str) -> bool:
    flags = str(version_info.get("flags", "")).lower()
    if "prerelease" in flags:
        return True

    properties = version_info.get("properties", [])
    if isinstance(properties, list):
        for prop in properties:
            if not isinstance(prop, dict):
                continue
            if prop.get("key") == "Microsoft.VisualStudio.Code.PreRelease":
                return str(prop.get("value", "")).lower() == "true"

    if PRERELEASE_TOKEN_RE.search(version):
        return True
    if PRERELEASE_SUFFIX_RE.search(version):
        return True
    return False


def pick_latest_version(versions: list[dict[str, Any]], include_prerelease: bool) -> str | None:
    for version_info in versions:
        version = version_info.get("version")
        if not isinstance(version, str):
            continue
        if not include_prerelease and is_prerelease(version_info, version):
            continue
        return version
    return None


def fetch_latest_info(
    publisher: str,
    name: str,
    include_prerelease: bool,
) -> tuple[str | None, str | None, str | None]:
    extension_id = f"{publisher}.{name}"
    payload = {
        "filters": [
            {
                "criteria": [
                    {"filterType": 7, "value": extension_id},
                ]
            }
        ],
        "flags": 119,
    }
    request = urllib.request.Request(
        MARKETPLACE_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers=MARKETPLACE_HEADERS,
        method="POST",
    )

    try:
        data = read_json_with_retries(request)
    except Exception as err:
        raise UpdateError(f"Failed to query the Marketplace for {extension_id}: {err}") from err

    extension = data.get("results", [{}])[0].get("extensions", [{}])[0]
    if not isinstance(extension, dict) or not extension:
        raise UpdateError(f"Marketplace metadata was not found for {extension_id}.")

    latest = pick_latest_version(extension.get("versions", []), include_prerelease)
    publisher_api = extension.get("publisher", {}).get("publisherName")
    name_api = extension.get("extensionName")
    return latest, publisher_api, name_api


def compute_hash(publisher: str, name: str, version: str, target_platform: str = "") -> str:
    target_platform_suffix = f"?targetPlatform={target_platform}" if target_platform else ""
    url = (
        f"https://{publisher}.gallery.vsassets.io/_apis/public/gallery/publisher/"
        f"{publisher}/extension/{name}/{version}/assetbyname/"
        f"Microsoft.VisualStudio.Services.VSIXPackage{target_platform_suffix}"
    )
    proc = subprocess.run(
        ["nix", "store", "prefetch-file", "--json", "--hash-type", "sha256", url],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.strip()
        raise UpdateError(
            f"Failed to prefetch {publisher}.{name}@{version}: {stderr or 'nix store prefetch-file failed.'}"
        )

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as err:
        raise UpdateError(f"Failed to parse hash output for {publisher}.{name}@{version}.") from err

    hash_value = data.get("hash")
    if not isinstance(hash_value, str) or not hash_value:
        raise UpdateError(f"Missing hash for {publisher}.{name}@{version}.")
    return hash_value


GALLERY_BASE_URL = os.getenv("VSCODE_GALLERY_BASE_URL")


def probe_vsix_available(
    publisher: str,
    name: str,
    version: str,
    target_platform: str | None = None,
) -> bool:
    query = f"?targetPlatform={target_platform}" if target_platform else ""
    if GALLERY_BASE_URL:
        url = f"{GALLERY_BASE_URL}/{publisher}/extension/{name}/{version}/{query}"
    else:
        url = (
            f"https://{publisher}.gallery.vsassets.io/_apis/public/gallery/publisher/"
            f"{publisher}/extension/{name}/{version}/assetbyname/"
            f"Microsoft.VisualStudio.Services.VSIXPackage{query}"
        )
    req = urllib.request.Request(url, headers={"Range": "bytes=0-0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status == 200
    except urllib.error.HTTPError:
        return False
    except Exception:
        return False


def probe_generic_vsix_available(publisher: str, name: str, version: str) -> bool:
    return probe_vsix_available(publisher, name, version)


def fetch_target_platforms(
    publisher: str,
    name: str,
    version: str,
) -> list[str]:
    available = []
    for target_platform in NIX_SYSTEM_TO_TARGET_PLATFORM.values():
        if probe_vsix_available(publisher, name, version, target_platform):
            available.append(target_platform)
    return available


def systems_for_target_platforms(target_platforms: list[str]) -> dict[str, str]:
    return {
        nix_system: target_platform
        for nix_system, target_platform in NIX_SYSTEM_TO_TARGET_PLATFORM.items()
        if target_platform in target_platforms
    }


def compute_per_system_hashes(
    publisher: str,
    name: str,
    version: str,
    system_platforms: dict[str, str],
) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for nix_system, target_platform in system_platforms.items():
        hashes[nix_system] = compute_hash(publisher, name, version, target_platform)
    return hashes


def compute_locked_hash(
    publisher: str,
    name: str,
    version: str,
    generic_available: bool,
    target_platforms: list[str],
    legacy_arch: str | None,
) -> HashValue:
    system_platforms = systems_for_target_platforms(target_platforms)
    if system_platforms:
        hashes = compute_per_system_hashes(publisher, name, version, system_platforms)
        if generic_available:
            return {
                DEFAULT_SHA256_KEY: compute_hash(publisher, name, version),
                **hashes,
            }
        return hashes

    if generic_available:
        return compute_hash(publisher, name, version)

    if legacy_arch:
        matching_systems = {
            nix_system: target_platform
            for nix_system, target_platform in NIX_SYSTEM_TO_TARGET_PLATFORM.items()
            if target_platform == legacy_arch
        }
        if matching_systems:
            return compute_per_system_hashes(publisher, name, version, matching_systems)

    raise UpdateError(f"No downloadable VSIX was found for {publisher}.{name}@{version}.")


def is_hash_multi_arch(value: Any) -> bool:
    return isinstance(value, dict)


def needs_normalization(
    entry: dict[str, Any],
    current_sha256: Any,
    generic_available: bool,
    target_platforms: list[str],
) -> bool:
    if "arch" in entry:
        return True

    expected_system_keys = set(systems_for_target_platforms(target_platforms))
    if not expected_system_keys:
        return generic_available and isinstance(current_sha256, dict)

    if not isinstance(current_sha256, dict):
        return True

    expected_keys = set(expected_system_keys)
    if generic_available:
        expected_keys.add(DEFAULT_SHA256_KEY)
    return set(current_sha256) != expected_keys


def iter_entries(data: Any, selected_groups: list[str]) -> list[tuple[int, str | None, dict[str, Any]]]:
    entries: list[tuple[int, str | None, dict[str, Any]]] = []
    index = 0

    if isinstance(data, list):
        if selected_groups:
            raise UpdateError("--group can only be used when the lock file root is an attribute set.")
        for entry in data:
            if not isinstance(entry, dict):
                raise UpdateError("Every extension entry must be a JSON object.")
            entries.append((index, None, entry))
            index += 1
        return entries

    if not isinstance(data, dict):
        raise UpdateError("Lock file root must be either a JSON list or a JSON object of lists.")

    group_names = selected_groups or list(data.keys())
    missing_groups = [group for group in group_names if group not in data]
    if missing_groups:
        raise UpdateError(f"Unknown groups: {', '.join(missing_groups)}")

    for group in group_names:
        group_entries = data[group]
        if not isinstance(group_entries, list):
            raise UpdateError(f"Group '{group}' must contain a JSON list.")
        for entry in group_entries:
            if not isinstance(entry, dict):
                raise UpdateError(f"Every extension entry in group '{group}' must be a JSON object.")
            entries.append((index, group, entry))
            index += 1
    return entries


def resolve_entry_update(index: int, group: str | None, entry: dict[str, Any], include_prerelease: bool, force: bool) -> dict[str, Any] | None:
    publisher = entry.get("publisher")
    name = entry.get("name")
    current_version = entry.get("version")
    current_sha256 = entry.get("sha256", "")
    legacy_arch = entry.get("arch")

    if not isinstance(publisher, str) or not publisher:
        raise UpdateError("Every extension entry must define a non-empty string 'publisher'.")
    if not isinstance(name, str) or not name:
        raise UpdateError("Every extension entry must define a non-empty string 'name'.")
    if not isinstance(current_version, str) or not current_version:
        raise UpdateError(f"Extension {publisher}.{name} must define a non-empty string 'version'.")
    if isinstance(current_sha256, dict):
        for nix_system, hash_value in current_sha256.items():
            if not isinstance(nix_system, str) or not nix_system:
                raise UpdateError(
                    f"Extension {publisher}.{name} has a sha256 map with a non-string key."
                )
            if not isinstance(hash_value, str) or not hash_value:
                raise UpdateError(
                    f"Extension {publisher}.{name} has an invalid sha256 for key '{nix_system}'."
                )
    elif not isinstance(current_sha256, str):
        raise UpdateError(f"Extension {publisher}.{name} has a non-string, non-object 'sha256' field.")

    if legacy_arch is not None and not isinstance(legacy_arch, (str, dict)):
        raise UpdateError(f"Extension {publisher}.{name} has an invalid 'arch' field.")

    entry_prerelease = entry.get("prerelease")
    if entry_prerelease is None:
        allow_prerelease = include_prerelease
    elif isinstance(entry_prerelease, bool):
        allow_prerelease = entry_prerelease
    else:
        raise UpdateError(f"Extension {publisher}.{name} has a non-boolean 'prerelease' field.")

    latest_version, publisher_api, name_api = fetch_latest_info(publisher, name, allow_prerelease)
    if latest_version is None:
        return None

    download_publisher = publisher_api if isinstance(publisher_api, str) and publisher_api else publisher
    download_name = name_api if isinstance(name_api, str) and name_api else name

    generic_available = probe_generic_vsix_available(download_publisher, download_name, latest_version)
    target_platforms = fetch_target_platforms(download_publisher, download_name, latest_version)
    normalize_entry = needs_normalization(entry, current_sha256, generic_available, target_platforms)

    if latest_version == current_version and not normalize_entry and not force:
        return None

    latest_hash = compute_locked_hash(
        download_publisher,
        download_name,
        latest_version,
        generic_available,
        target_platforms,
        legacy_arch if isinstance(legacy_arch, str) else None,
    )

    return {
        "index": index,
        "group": group,
        "publisher": publisher,
        "name": name,
        "current_version": current_version,
        "latest_version": latest_version,
        "latest_hash": latest_hash,
        "is_multi_arch": isinstance(latest_hash, dict),
        "generic_available": generic_available,
        "normalized": normalize_entry,
        "target_platforms": target_platforms,
    }


def write_json_atomic(path: Path, data: Any) -> None:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        tmp_path = Path(handle.name)
        json.dump(data, handle, indent=2)
        handle.write("\n")
    tmp_path.replace(path)


def format_update(update: dict[str, Any]) -> str:
    group = update["group"]
    prefix = f"[{group}] " if group is not None else ""
    version_line = (
        f"{prefix}{update['publisher']}.{update['name']}: "
        f"{update['current_version']} -> {update['latest_version']}"
    )
    if update.get("is_multi_arch"):
        hashes = update["latest_hash"]
        hash_lines = "\n".join(
            f"  {system}: {h}"
            for system, h in sorted(
                hashes.items(),
                key=lambda item: (item[0] != DEFAULT_SHA256_KEY, item[0]),
            )
        )
        result = f"{version_line}\n{hash_lines}"
        if update.get("generic_available") and update["target_platforms"]:
            platforms = ", ".join(update["target_platforms"])
            result += f"\n  (generic fallback plus target platforms: {platforms})"
        elif update["target_platforms"]:
            platforms = ", ".join(update["target_platforms"])
            result += f"\n  (target platforms: {platforms})"
        return result
    return f"{version_line} ({update['latest_hash']})"


def main() -> int:
    args = parse_args()
    if args.jobs < 1:
        raise UpdateError("--jobs must be at least 1.")

    path = Path(args.path)
    data = read_json(path)
    entries = iter_entries(data, args.group)

    if not entries:
        print("No Marketplace extensions were found in the lock file.")
        return 0

    updates: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as executor:
        futures = [
            executor.submit(resolve_entry_update, index, group, entry, args.include_prerelease, args.force)
            for index, group, entry in entries
        ]
        for future in concurrent.futures.as_completed(futures):
            update = future.result()
            if update is not None:
                updates.append(update)

    updates.sort(key=lambda update: update["index"])

    if not updates:
        print("All Marketplace extensions are already up to date.")
        return 0

    for update in updates:
        print(format_update(update))

    if args.check:
        print(f"\n{len(updates)} update(s) available.")
        return 1

    update_map = {
        (update["group"], update["publisher"], update["name"]): update
        for update in updates
    }

    if isinstance(data, list):
        for entry in data:
            key = (None, entry["publisher"], entry["name"])
            update = update_map.get(key)
            if update is not None:
                entry["version"] = update["latest_version"]
                entry["sha256"] = update["latest_hash"]
                entry.pop("arch", None)
    else:
        selected_groups = args.group or list(data.keys())
        for group in selected_groups:
            for entry in data[group]:
                key = (group, entry["publisher"], entry["name"])
                update = update_map.get(key)
                if update is not None:
                    entry["version"] = update["latest_version"]
                    entry["sha256"] = update["latest_hash"]
                    entry.pop("arch", None)

    write_json_atomic(path, data)
    print(f"\nUpdated {len(updates)} extension(s) in {path}.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except UpdateError as err:
        print(f"error: {err}", file=sys.stderr)
        raise SystemExit(1)
