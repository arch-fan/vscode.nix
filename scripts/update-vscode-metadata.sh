#!/usr/bin/env bash
set -euo pipefail

repo_root="${REPO_ROOT:-$PWD}"
cd "$repo_root"

if [[ ! -f "versions.json" ]]; then
  echo "versions.json not found in: $repo_root" >&2
  exit 1
fi

systems=(
  "x86_64-linux"
  "aarch64-linux"
  "armv7l-linux"
  "x86_64-darwin"
  "aarch64-darwin"
)

declare -A platform_for_system=(
  ["x86_64-linux"]="linux-x64"
  ["aarch64-linux"]="linux-arm64"
  ["armv7l-linux"]="linux-armhf"
  ["x86_64-darwin"]="darwin"
  ["aarch64-darwin"]="darwin-arm64"
)

api_url() {
  local platform="$1"
  local quality="$2"
  echo "https://update.code.visualstudio.com/api/update/${platform}/${quality}/latest"
}

fetch_release_json() {
  local platform="$1"
  local quality="$2"
  curl --fail --silent --show-error "$(api_url "$platform" "$quality")"
}

to_sri() {
  local hex="$1"
  nix hash convert --to sri --hash-algo sha256 "$hex"
}

build_channel_json() {
  local quality="$1"

  local baseline_json baseline_version baseline_rev
  baseline_json="$(fetch_release_json "linux-x64" "$quality")"
  baseline_version="$(jq --raw-output '.name' <<<"$baseline_json")"
  baseline_rev="$(jq --raw-output '.version' <<<"$baseline_json")"

  if [[ -z "$baseline_version" || "$baseline_version" == "null" ]]; then
    echo "Could not read version for quality: $quality" >&2
    exit 1
  fi

  if [[ -z "$baseline_rev" || "$baseline_rev" == "null" ]]; then
    echo "Could not read commit revision for quality: $quality" >&2
    exit 1
  fi

  local server_json server_hex server_sri
  server_json="$(fetch_release_json "server-linux-x64" "$quality")"
  server_hex="$(jq --raw-output '.sha256hash' <<<"$server_json")"
  server_sri="$(to_sri "$server_hex")"

  local hashes_json='{}'
  local system platform response version rev hex sri
  for system in "${systems[@]}"; do
    platform="${platform_for_system[$system]}"
    response="$(fetch_release_json "$platform" "$quality")"
    version="$(jq --raw-output '.name' <<<"$response")"
    rev="$(jq --raw-output '.version' <<<"$response")"

    if [[ "$version" != "$baseline_version" || "$rev" != "$baseline_rev" ]]; then
      echo "Metadata mismatch for ${quality}/${system}: got ${version}@${rev}, expected ${baseline_version}@${baseline_rev}" >&2
      exit 1
    fi

    hex="$(jq --raw-output '.sha256hash' <<<"$response")"
    sri="$(to_sri "$hex")"
    hashes_json="$(jq --compact-output --arg k "$system" --arg v "$sri" '. + {($k): $v}' <<<"$hashes_json")"
  done

  jq -n \
    --arg version "$baseline_version" \
    --arg rev "$baseline_rev" \
    --arg serverHash "$server_sri" \
    --argjson hashes "$hashes_json" \
    '{
      version: $version,
      rev: $rev,
      serverHash: $serverHash,
      hashes: $hashes
    }'
}

stable_json="$(build_channel_json "stable")"
insiders_json="$(build_channel_json "insider")"

tmp_file="$(mktemp)"
jq -n \
  --argjson stable "$stable_json" \
  --argjson insiders "$insiders_json" \
  '{
    stable: $stable,
    insiders: $insiders
  }' > "$tmp_file"

mv "$tmp_file" versions.json
echo "Updated versions.json"
cat versions.json
