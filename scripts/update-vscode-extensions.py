#!/usr/bin/env python3

from __future__ import annotations

import base64
import concurrent.futures
import contextlib
import hashlib
import http.client
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import click
from voluptuous import ALLOW_EXTRA, All, Length, MultipleInvalid, Optional, Required, Schema
from voluptuous import Any as OneOf

MARKETPLACE_URL = os.getenv(
    "VSCODE_MARKETPLACE_URL",
    "https://marketplace.visualstudio.com/_apis/public/gallery/extensionquery",
)
MARKETPLACE_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json;api-version=7.1-preview.1",
}
DOWNLOAD_HEADERS = {
    "User-Agent": "vscode.nix-extension-updater",
}
PRERELEASE_TOKEN_RE = re.compile(r"(?i)(^|[.-])(alpha|beta|rc|pre|preview)([.-]|$)")
PRERELEASE_SUFFIX_RE = re.compile(r"-[0-9A-Za-z]")
# Downloads are network-bound, so oversubscribe cores by default.
DEFAULT_JOBS = min(16, max(4, (os.cpu_count() or 4) * 2))
DEFAULT_SHA256_KEY = "default"
DOWNLOAD_CHUNK = 1 << 16
DOWNLOAD_TIMEOUT = 120
RETRY_BACKOFF = 0.5

NIX_SYSTEM_TO_TARGET_PLATFORM = {
    "x86_64-linux": "linux-x64",
    "aarch64-linux": "linux-arm64",
    "armv7l-linux": "linux-armhf",
    "x86_64-darwin": "darwin",
    "aarch64-darwin": "darwin-arm64",
}

GALLERY_BASE_URL = os.getenv("VSCODE_GALLERY_BASE_URL")

HashValue = str | dict[str, str]


class UpdateError(RuntimeError):
    """Raised when the lock file cannot be processed safely."""


@dataclass
class PendingUpdate:
    index: int
    group: str | None
    publisher: str
    name: str
    download_publisher: str
    download_name: str
    current_version: str
    latest_version: str
    generic_available: bool = False
    target_platforms: list[str] = field(default_factory=list)
    latest_hash: HashValue | None = None

    def asset_count(self) -> int:
        return (1 if self.generic_available else 0) + len(self.target_platforms)


@dataclass
class MarketplaceInfo:
    selected_version: str | None
    publisher: str | None
    name: str | None
    generic_available: bool
    target_platforms: list[str]
    newest_version: str | None
    newest_platforms: list[str] = field(default_factory=list)


@dataclass
class PendingResolution:
    pending: PendingUpdate | None
    warning: str | None = None


@dataclass
class UpdateFailure:
    index: int
    group: str | None
    publisher: str
    name: str
    message: str


@dataclass
class ResolveResult:
    pending_updates: list[PendingUpdate]
    warnings: list[str]
    failures: list[UpdateFailure]


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
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as err:
            last_error = err
        if attempt + 1 < attempts:
            time.sleep(RETRY_BACKOFF * (2**attempt))
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


def iter_candidate_versions(
    versions: list[dict[str, Any]], include_prerelease: bool
) -> list[str]:
    """Return published versions in Marketplace order, without duplicates."""
    candidates: list[str] = []
    seen: set[str] = set()
    for version_info in versions:
        version = version_info.get("version")
        if not isinstance(version, str):
            continue
        if version in seen:
            continue
        seen.add(version)
        if not include_prerelease and is_prerelease(version_info, version):
            continue
        candidates.append(version)
    return candidates


def pick_latest_version(versions: list[dict[str, Any]], include_prerelease: bool) -> str | None:
    candidates = iter_candidate_versions(versions, include_prerelease)
    return candidates[0] if candidates else None


def pick_latest_supported_version(
    versions: list[dict[str, Any]], include_prerelease: bool
) -> tuple[str | None, bool, list[str]]:
    """Pick the newest candidate that has a generic or supported target asset.

    Marketplace publishes each target platform as a separate version object. A
    release can therefore briefly appear with only an unsupported platform (for
    example, Alpine) while the usable assets are still being published. Do not
    let that partial release block the lock-file update.
    """
    for version in iter_candidate_versions(versions, include_prerelease):
        generic_available, target_platforms = collect_assets(versions, version)
        if generic_available or target_platforms:
            return version, generic_available, target_platforms
    return None, False, []


TARGET_PLATFORM_TO_NIX_SYSTEM = {
    target_platform: nix_system
    for nix_system, target_platform in NIX_SYSTEM_TO_TARGET_PLATFORM.items()
}


def collect_assets(versions: list[dict[str, Any]], latest_version: str) -> tuple[bool, list[str]]:
    """From the version objects sharing ``latest_version``, read which assets exist.

    The Marketplace lists one object per published target platform (a generic build
    simply omits ``targetPlatform``), so availability comes straight from metadata and
    no speculative download probes are needed.
    """
    generic_available = False
    target_platforms: list[str] = []
    for version_info in versions:
        if version_info.get("version") != latest_version:
            continue
        target_platform = version_info.get("targetPlatform")
        if not target_platform:
            generic_available = True
        elif target_platform in TARGET_PLATFORM_TO_NIX_SYSTEM and target_platform not in target_platforms:
            target_platforms.append(target_platform)
    return generic_available, target_platforms


def collect_published_platforms(versions: list[dict[str, Any]], version: str) -> list[str]:
    """Return the raw Marketplace platform labels published for a version."""
    platforms: list[str] = []
    for version_info in versions:
        if version_info.get("version") != version:
            continue
        platform = version_info.get("targetPlatform") or "generic"
        if isinstance(platform, str) and platform not in platforms:
            platforms.append(platform)
    return platforms


def fetch_latest_info(
    publisher: str,
    name: str,
    include_prerelease: bool,
) -> MarketplaceInfo:
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

    versions = extension.get("versions", [])
    publisher_api = extension.get("publisher", {}).get("publisherName")
    name_api = extension.get("extensionName")
    latest = pick_latest_version(versions, include_prerelease)
    if latest is None:
        return MarketplaceInfo(None, publisher_api, name_api, False, [], None)
    supported_version, generic_available, target_platforms = pick_latest_supported_version(
        versions, include_prerelease
    )
    if supported_version is None:
        # Preserve the existing, actionable error from resolve_pending when no
        # candidate has an asset for any system supported by this flake.
        return MarketplaceInfo(
            latest,
            publisher_api,
            name_api,
            False,
            [],
            latest,
            collect_published_platforms(versions, latest),
        )
    return MarketplaceInfo(
        supported_version,
        publisher_api,
        name_api,
        generic_available,
        target_platforms,
        latest,
        collect_published_platforms(versions, latest),
    )


def build_download_url(publisher: str, name: str, version: str, target_platform: str | None) -> str:
    query = f"?targetPlatform={target_platform}" if target_platform else ""
    if GALLERY_BASE_URL:
        return f"{GALLERY_BASE_URL}/{publisher}/extension/{name}/{version}/{query}"
    return (
        f"https://{publisher}.gallery.vsassets.io/_apis/public/gallery/publisher/"
        f"{publisher}/extension/{name}/{version}/assetbyname/"
        f"Microsoft.VisualStudio.Services.VSIXPackage{query}"
    )


def stream_sha256(
    publisher: str,
    name: str,
    version: str,
    target_platform: str | None = None,
    attempts: int = 3,
) -> str | None:
    """Download the VSIX and hash it as the bytes arrive, without ever writing it to disk.

    Returns the flat sha256 in SRI form (``sha256-<base64>``), or ``None`` when the asset
    does not exist for this target platform (HTTP 404).
    """
    url = build_download_url(publisher, name, version, target_platform)
    last_error: Exception | None = None
    for attempt in range(attempts):
        digest = hashlib.sha256()
        try:
            request = urllib.request.Request(url, headers=DOWNLOAD_HEADERS)
            with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT) as response:
                while True:
                    chunk = response.read(DOWNLOAD_CHUNK)
                    if not chunk:
                        break
                    digest.update(chunk)
            return "sha256-" + base64.b64encode(digest.digest()).decode("ascii")
        except urllib.error.HTTPError as err:
            if err.code == 404:
                return None
            last_error = err
        except (urllib.error.URLError, TimeoutError, http.client.HTTPException, ConnectionError) as err:
            last_error = err
        if attempt + 1 < attempts:
            time.sleep(RETRY_BACKOFF * (2**attempt))

    platform_note = f" ({target_platform})" if target_platform else ""
    raise UpdateError(
        f"Failed to download {publisher}.{name}@{version}{platform_note}: {last_error}"
    )


NON_EMPTY_STR = All(str, Length(min=1))

# A single lock entry. Unknown keys are preserved (ALLOW_EXTRA) so the updater only
# ever touches version/sha256/arch and rewrites everything else verbatim.
ENTRY_SCHEMA = Schema(
    {
        Required("publisher"): NON_EMPTY_STR,
        Required("name"): NON_EMPTY_STR,
        Required("version"): NON_EMPTY_STR,
        Required("sha256"): OneOf(NON_EMPTY_STR, {NON_EMPTY_STR: NON_EMPTY_STR}),
        Optional("prerelease"): bool,
        Optional("arch"): OneOf(str, dict),
    },
    extra=ALLOW_EXTRA,
)


def validate_entry(entry: dict[str, Any]) -> dict[str, Any]:
    try:
        return ENTRY_SCHEMA(entry)
    except MultipleInvalid as err:
        publisher = entry.get("publisher")
        name = entry.get("name")
        label = (
            f" for {publisher}.{name}"
            if isinstance(publisher, str) and isinstance(name, str)
            else ""
        )
        raise UpdateError(f"Invalid extension entry{label}: {err}") from err


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


def resolve_pending(
    index: int,
    group: str | None,
    entry: dict[str, Any],
    include_prerelease: bool,
    force: bool,
) -> PendingResolution:
    """Decide whether an entry needs updating without touching the download endpoint.

    When the newest published version already matches the pinned version we return early
    (unless --force), so unchanged extensions never trigger a download or a hash.
    """
    validated = validate_entry(entry)
    publisher = validated["publisher"]
    name = validated["name"]
    current_version = validated["version"]

    entry_prerelease = validated.get("prerelease")
    allow_prerelease = include_prerelease if entry_prerelease is None else entry_prerelease

    info = fetch_latest_info(publisher, name, allow_prerelease)
    latest_version = info.selected_version
    if latest_version is None:
        return PendingResolution(None)

    warning = None
    if info.newest_version is not None and info.newest_version != latest_version:
        published_platforms = ", ".join(info.newest_platforms) or "none"
        warning = (
            f"{publisher}.{name}: newest published version {info.newest_version} has no VSIX "
            f"for a supported system (published platforms: {published_platforms}); "
            f"using {latest_version}."
        )

    if latest_version == current_version and not force:
        return PendingResolution(None, warning)

    if not info.generic_available and not info.target_platforms:
        raise UpdateError(
            f"No VSIX for a supported system was published for {publisher}.{name}@{latest_version}."
        )

    download_publisher = info.publisher if isinstance(info.publisher, str) and info.publisher else publisher
    download_name = info.name if isinstance(info.name, str) and info.name else name

    return PendingResolution(
        PendingUpdate(
            index=index,
            group=group,
            publisher=publisher,
            name=name,
            download_publisher=download_publisher,
            download_name=download_name,
            current_version=current_version,
            latest_version=latest_version,
            generic_available=info.generic_available,
            target_platforms=info.target_platforms,
        ),
        warning,
    )


def hash_targets(pending: PendingUpdate) -> list[tuple[str, str | None]]:
    """The assets to download for one update, taken from what the metadata declared."""
    targets: list[tuple[str, str | None]] = []
    if pending.generic_available:
        targets.append((DEFAULT_SHA256_KEY, None))
    for target_platform in pending.target_platforms:
        targets.append((TARGET_PLATFORM_TO_NIX_SYSTEM[target_platform], target_platform))
    return targets


def assemble_hash(pending: PendingUpdate, results: dict[str, str | None]) -> HashValue:
    generic = results.get(DEFAULT_SHA256_KEY)
    platform_hashes = {
        nix_system: value
        for nix_system, value in results.items()
        if nix_system != DEFAULT_SHA256_KEY and value
    }

    if platform_hashes:
        latest: dict[str, str] = {}
        if generic:
            latest[DEFAULT_SHA256_KEY] = generic
        for nix_system in NIX_SYSTEM_TO_TARGET_PLATFORM:
            if nix_system in platform_hashes:
                latest[nix_system] = platform_hashes[nix_system]
        return latest

    if generic:
        return generic

    raise UpdateError(
        f"No downloadable VSIX was found for "
        f"{pending.download_publisher}.{pending.download_name}@{pending.latest_version}."
    )


def resolve_and_hash(
    entries: list[tuple[int, str | None, dict[str, Any]]],
    include_prerelease: bool,
    force: bool,
    jobs: int,
    check: bool,
    on_progress: Callable[[], None] | None = None,
    on_progress_start: Callable[[int], None] | None = None,
) -> ResolveResult:
    """Resolve versions and hash assets in one shared, fully pipelined pool.

    An extension's download/hash tasks (one per declared platform asset) are submitted
    the moment its metadata resolves, so downloads run while other extensions are still
    being version-checked and every asset across all extensions hashes concurrently.
    With --check no download task is ever submitted and progress fires once per
    checked entry. During an update, progress starts once the number of extensions
    requiring hashes is known, then fires when each extension's last asset finishes.
    A failed extension is recorded and skipped so other entries can still be updated.
    """

    def bump() -> None:
        if on_progress is not None:
            on_progress()

    pending_updates: list[PendingUpdate] = []
    warnings: list[tuple[int, str]] = []
    failures: list[UpdateFailure] = []
    results: dict[int, dict[str, str | None]] = {}
    remaining_assets: dict[int, int] = {}
    failed_pending_ids: set[int] = set()

    def append_failure(
        index: int,
        group: str | None,
        entry: dict[str, Any],
        error: Exception,
    ) -> None:
        publisher = entry.get("publisher")
        name = entry.get("name")
        failures.append(
            UpdateFailure(
                index=index,
                group=group,
                publisher=publisher if isinstance(publisher, str) and publisher else "<unknown>",
                name=name if isinstance(name, str) and name else "<unknown>",
                message=str(error) or error.__class__.__name__,
            )
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as executor:
        resolve_futures = {
            executor.submit(resolve_pending, index, group, entry, include_prerelease, force): (
                index,
                group,
                entry,
            )
            for index, group, entry in entries
        }
        hash_futures: dict[concurrent.futures.Future, tuple[PendingUpdate, str]] = {}

        for future in concurrent.futures.as_completed(resolve_futures):
            index, group, entry = resolve_futures[future]
            try:
                resolution = future.result()
            except Exception as err:
                append_failure(index, group, entry, err)
                if check:
                    bump()
                continue

            if resolution.warning is not None:
                warnings.append((index, resolution.warning))

            pending = resolution.pending
            if pending is None:
                if check:
                    bump()
                continue
            pending_updates.append(pending)
            if check:
                bump()
                continue
            results[id(pending)] = {}
            remaining_assets[id(pending)] = pending.asset_count()
            for key, target_platform in hash_targets(pending):
                hash_future = executor.submit(
                    stream_sha256,
                    pending.download_publisher,
                    pending.download_name,
                    pending.latest_version,
                    target_platform,
                )
                hash_futures[hash_future] = (pending, key)

        if not check and pending_updates and on_progress_start is not None:
            on_progress_start(len(pending_updates))

        for future in concurrent.futures.as_completed(hash_futures):
            pending, key = hash_futures[future]
            pending_id = id(pending)
            try:
                results[pending_id][key] = future.result()
            except Exception as err:
                if pending_id not in failed_pending_ids:
                    failed_pending_ids.add(pending_id)
                    failures.append(
                        UpdateFailure(
                            index=pending.index,
                            group=pending.group,
                            publisher=pending.publisher,
                            name=pending.name,
                            message=str(err) or err.__class__.__name__,
                        )
                    )
            remaining_assets[pending_id] -= 1
            if remaining_assets[pending_id] == 0:
                bump()

    if not check:
        successful_updates: list[PendingUpdate] = []
        for pending in pending_updates:
            if id(pending) in failed_pending_ids:
                continue
            try:
                pending.latest_hash = assemble_hash(pending, results[id(pending)])
            except Exception as err:
                failures.append(
                    UpdateFailure(
                        index=pending.index,
                        group=pending.group,
                        publisher=pending.publisher,
                        name=pending.name,
                        message=str(err) or err.__class__.__name__,
                    )
                )
                continue
            successful_updates.append(pending)
        pending_updates = successful_updates

    pending_updates.sort(key=lambda pending: pending.index)
    failures.sort(key=lambda failure: failure.index)
    warnings.sort(key=lambda warning: warning[0])
    return ResolveResult(
        pending_updates=pending_updates,
        warnings=[warning for _, warning in warnings],
        failures=failures,
    )


def write_json_atomic(path: Path, data: Any) -> None:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        tmp_path = Path(handle.name)
        json.dump(data, handle, indent=2)
        handle.write("\n")
    tmp_path.replace(path)


def format_version_line(pending: PendingUpdate) -> str:
    prefix = click.style(f"[{pending.group}] ", fg="cyan") if pending.group is not None else ""
    name = click.style(f"{pending.publisher}.{pending.name}", bold=True)
    old = click.style(pending.current_version, fg="red")
    arrow = click.style("→", fg="bright_black")
    new = click.style(pending.latest_version, fg="green", bold=True)
    return f"{prefix}{name}  {old} {arrow} {new}"


def format_failure(failure: UpdateFailure) -> str:
    prefix = click.style(f"[{failure.group}] ", fg="cyan") if failure.group is not None else ""
    name = click.style(f"{failure.publisher}.{failure.name}", bold=True)
    return f"{prefix}{name}: {failure.message}"


def format_update(pending: PendingUpdate) -> str:
    version_line = format_version_line(pending)
    latest_hash = pending.latest_hash
    if isinstance(latest_hash, dict):
        hash_lines = "\n".join(
            f"    {click.style(system, fg='cyan')}: {click.style(value, fg='bright_black')}"
            for system, value in sorted(
                latest_hash.items(),
                key=lambda item: (item[0] != DEFAULT_SHA256_KEY, item[0]),
            )
        )
        result = f"{version_line}\n{hash_lines}"
        platforms = [
            NIX_SYSTEM_TO_TARGET_PLATFORM[system]
            for system in latest_hash
            if system != DEFAULT_SHA256_KEY
        ]
        if DEFAULT_SHA256_KEY in latest_hash and platforms:
            note = f"generic fallback plus target platforms: {', '.join(platforms)}"
        elif platforms:
            note = f"target platforms: {', '.join(platforms)}"
        else:
            note = ""
        if note:
            result += "\n" + click.style(f"    ({note})", fg="bright_black")
        return result
    return f"{version_line}  {click.style(latest_hash, fg='bright_black')}"


def apply_updates(data: Any, pending_updates: list[PendingUpdate], selected_groups: list[str]) -> None:
    update_map = {
        (pending.group, pending.publisher, pending.name): pending
        for pending in pending_updates
    }

    if isinstance(data, list):
        groups: list[tuple[str | None, list[Any]]] = [(None, data)]
    else:
        chosen = selected_groups or list(data.keys())
        groups = [(group, data[group]) for group in chosen]

    for group, group_entries in groups:
        for entry in group_entries:
            pending = update_map.get((group, entry.get("publisher"), entry.get("name")))
            if pending is not None:
                entry["version"] = pending.latest_version
                entry["sha256"] = pending.latest_hash
                entry.pop("arch", None)


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("path", type=click.Path(dir_okay=False, path_type=Path))
@click.option(
    "--check",
    is_flag=True,
    help="Print pending updates without writing the file. Exits with code 1 when updates are available.",
)
@click.option(
    "--include-prerelease",
    is_flag=True,
    help="Allow prerelease versions for every extension unless an entry sets prerelease = false.",
)
@click.option(
    "--group",
    "groups",
    multiple=True,
    metavar="NAME",
    help="Limit updates to one or more groups in a grouped lock file. Repeat the flag to select multiple groups.",
)
@click.option(
    "--jobs",
    type=click.IntRange(min=1),
    default=DEFAULT_JOBS,
    show_default=True,
    metavar="N",
    help="Maximum number of concurrent download/hash jobs.",
)
@click.option(
    "--force",
    is_flag=True,
    help="Recompute hashes for all entries regardless of whether the version changed.",
)
@click.pass_context
def main(
    ctx: click.Context,
    path: Path,
    check: bool,
    include_prerelease: bool,
    groups: tuple[str, ...],
    jobs: int,
    force: bool,
) -> None:
    """Update pinned VS Code Marketplace extensions in a JSON lock file.

    The file may be either a flat list or an attribute set of groups.
    """
    selected_groups = list(groups)
    try:
        data = read_json(path)
        entries = iter_entries(data, selected_groups)

        if not entries:
            click.secho("No Marketplace extensions were found in the lock file.", fg="yellow")
            return

        # Everything runs in one pipelined pool: version checks and downloads overlap,
        # and every VSIX asset hashes concurrently. In update mode the bar starts once
        # version resolution reveals how many extensions actually need hashes. It goes
        # to stderr and auto-hides when stderr is not a terminal, so piped/CI output
        # stays clean.
        with contextlib.ExitStack() as progress_stack:
            bar: click.ProgressBar | None = None

            def open_progress(action: str, length: int) -> click.ProgressBar:
                noun = "extension" if length == 1 else "extensions"
                label = click.style(f"{action} {length} {noun}", fg="cyan", bold=True)
                return progress_stack.enter_context(
                    click.progressbar(
                        length=length,
                        label=label,
                        file=sys.stderr,
                        fill_char="█",
                        empty_char="░",
                        bar_template="%(label)s  %(bar)s  %(info)s",
                        info_sep=" · ",
                        width=24,
                        show_eta=False,
                        show_pos=True,
                        show_percent=True,
                    )
                )

            if check:
                bar = open_progress("Checking", len(entries))

            def start_hash_progress(length: int) -> None:
                nonlocal bar
                bar = open_progress("Hashing", length)

            def bump_progress() -> None:
                if bar is not None:
                    bar.update(1)

            result = resolve_and_hash(
                entries,
                include_prerelease,
                force,
                jobs,
                check,
                on_progress=bump_progress,
                on_progress_start=None if check else start_hash_progress,
            )

        for warning in result.warnings:
            click.secho(f"warning: {warning}", err=True, fg="yellow")
        for failure in result.failures:
            click.secho(f"warning: {format_failure(failure)}", err=True, fg="yellow")

        pending_updates = result.pending_updates
        if not pending_updates:
            if result.failures:
                click.secho(
                    f"\n{len(result.failures)} extension(s) could not be updated.",
                    err=True,
                    fg="yellow",
                    bold=True,
                )
                ctx.exit(1)
            click.secho("✓ All Marketplace extensions are already up to date.", fg="green", bold=True)
            return

        # --check only reports which versions changed; resolve_and_hash never submitted
        # a download task, so it stays cheap even when many extensions have updates.
        if check:
            for pending in pending_updates:
                click.echo(format_version_line(pending))
            suffix = (
                f"; {len(result.failures)} extension(s) could not be checked"
                if result.failures
                else ""
            )
            click.secho(
                f"\n{len(pending_updates)} update(s) available{suffix}.",
                fg="yellow",
                bold=True,
            )
            ctx.exit(1)

        for pending in pending_updates:
            click.echo(format_update(pending))

        apply_updates(data, pending_updates, selected_groups)

        write_json_atomic(path, data)
        click.secho(
            f"\n✓ Updated {len(pending_updates)} extension(s) in {path}.", fg="green", bold=True
        )
        if result.failures:
            click.secho(
                f"warning: {len(result.failures)} extension(s) were left unchanged.",
                err=True,
                fg="yellow",
                bold=True,
            )
            ctx.exit(1)
    except UpdateError as err:
        click.secho(f"error: {err}", err=True, fg="red")
        ctx.exit(1)


if __name__ == "__main__":
    main()
