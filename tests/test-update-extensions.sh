#!/usr/bin/env bash
set -euo pipefail

script="$1"
fixtures_dir="$2"
server_script="$3"

tmpdir="$(mktemp -d)"
cleanup() {
  if [[ -n "${server_pid:-}" ]]; then
    kill "$server_pid" 2>/dev/null || true
    wait "$server_pid" 2>/dev/null || true
  fi
  rm -rf "$tmpdir"
}
trap cleanup EXIT

# SRI hash the updater produces for a VSIX whose body the mock serves as "<key>|<selected>".
sri() {
  python3 -c 'import sys, hashlib, base64; print("sha256-" + base64.b64encode(hashlib.sha256(sys.argv[1].encode()).digest()).decode())' "$1"
}

cp "$fixtures_dir/extensions-flat.json" "$tmpdir/flat.json"
cp "$fixtures_dir/extensions-flat.json" "$tmpdir/flat.original.json"
cat > "$tmpdir/grouped.json" <<'EOF'
{
  "base": [
    {
      "publisher": "gamma",
      "name": "three",
      "version": "1.0.0",
      "sha256": "sha256-gamma-three-1.0.0",
      "prerelease": false
    },
    {
      "publisher": "delta",
      "name": "four",
      "version": "1.0.0",
      "sha256": "sha256-delta-four-1.0.0"
    }
  ],
  "node": [
    {
      "publisher": "epsilon",
      "name": "five",
      "version": "1.0.0",
      "sha256": "sha256-epsilon-five-1.0.0",
      "arch": "linux-x64"
    },
    {
      "publisher": "eta",
      "name": "seven",
      "version": "3.0.0",
      "sha256": {
        "default": "sha256-eta-seven-3.0.0-generic",
        "x86_64-linux": "sha256-eta-seven-3.0.0-linux-x64"
      }
    }
  ],
  "native": [
    {
      "publisher": "zeta",
      "name": "six",
      "version": "2.0.0",
      "sha256": {
        "x86_64-linux": "sha256-zeta-six-2.0.0-linux-x64",
        "aarch64-linux": "sha256-zeta-six-2.0.0-linux-arm64"
      }
    }
  ]
}
EOF
cp "$tmpdir/grouped.json" "$tmpdir/grouped.original.json"

port_file="$tmpdir/port"
python3 "$server_script" "$fixtures_dir/marketplace-responses.json" "$port_file" "$fixtures_dir/vsix-platforms.json" &
server_pid="$!"

for _ in $(seq 1 50); do
  if [[ -s "$port_file" ]]; then
    break
  fi
  sleep 0.1
done

if [[ ! -s "$port_file" ]]; then
  echo "mock marketplace server did not start" >&2
  exit 1
fi

export VSCODE_MARKETPLACE_URL="http://127.0.0.1:$(cat "$port_file")"
export VSCODE_GALLERY_BASE_URL="http://127.0.0.1:$(cat "$port_file")"

# Expected hashes for the versions the updater resolves against the mock.
alpha_one="$(sri 'alpha/extension/one/2.0.0|default')"
beta_two="$(sri 'beta/extension/two/2.0.0-beta.1|default')"
delta_four="$(sri 'delta/extension/four/1.2.0|default')"
epsilon_five="$(sri 'epsilon/extension/five/2.0.0|linux-x64')"
eta_seven_default="$(sri 'eta/extension/seven/4.0.0|default')"
eta_seven_x64="$(sri 'eta/extension/seven/4.0.0|linux-x64')"
eta_seven_arm64="$(sri 'eta/extension/seven/4.0.0|linux-arm64')"
zeta_six_x64="$(sri 'zeta/extension/six/3.0.0|linux-x64')"
zeta_six_arm64="$(sri 'zeta/extension/six/3.0.0|linux-arm64')"

if python3 "$script" --check "$tmpdir/flat.json"; then
  echo "--check should exit with code 1 when updates exist" >&2
  exit 1
else
  status="$?"
  if [[ "$status" -ne 1 ]]; then
    echo "--check exited with $status, expected 1" >&2
    exit 1
  fi
fi

cmp -s "$tmpdir/flat.original.json" "$tmpdir/flat.json"

python3 "$script" --jobs 2 "$tmpdir/flat.json"
jq -e \
  --arg alpha_one "$alpha_one" \
  --arg beta_two "$beta_two" \
  '
  length == 2 and
  .[0].publisher == "alpha" and
  .[0].name == "one" and
  .[0].version == "2.0.0" and
  .[0].sha256 == $alpha_one and
  .[1].publisher == "beta" and
  .[1].name == "two" and
  .[1].version == "2.0.0-beta.1" and
  .[1].sha256 == $beta_two and
  .[1].prerelease == true
' "$tmpdir/flat.json" >/dev/null

python3 "$script" --group node "$tmpdir/grouped.json"
jq -e \
  --arg epsilon_five "$epsilon_five" \
  --arg eta_seven_default "$eta_seven_default" \
  --arg eta_seven_x64 "$eta_seven_x64" \
  --arg eta_seven_arm64 "$eta_seven_arm64" \
  '
  .base[0].version == "1.0.0" and
  .base[0].sha256 == "sha256-gamma-three-1.0.0" and
  .base[1].version == "1.0.0" and
  .base[1].sha256 == "sha256-delta-four-1.0.0" and
  .node[0].version == "2.0.0" and
  .node[0].sha256."x86_64-linux" == $epsilon_five and
  (.node[0] | has("arch") | not) and
  .node[1].version == "4.0.0" and
  .node[1].sha256.default == $eta_seven_default and
  .node[1].sha256."x86_64-linux" == $eta_seven_x64 and
  .node[1].sha256."aarch64-linux" == $eta_seven_arm64 and
  (.node[1] | has("arch") | not)
' "$tmpdir/grouped.json" >/dev/null

python3 "$script" --include-prerelease "$tmpdir/grouped.json"
jq -e \
  --arg delta_four "$delta_four" \
  --arg epsilon_five "$epsilon_five" \
  --arg eta_seven_default "$eta_seven_default" \
  --arg eta_seven_x64 "$eta_seven_x64" \
  --arg eta_seven_arm64 "$eta_seven_arm64" \
  --arg zeta_six_x64 "$zeta_six_x64" \
  --arg zeta_six_arm64 "$zeta_six_arm64" \
  '
  .base[0].version == "1.0.0" and
  .base[0].sha256 == "sha256-gamma-three-1.0.0" and
  .base[0].prerelease == false and
  .base[1].version == "1.2.0" and
  .base[1].sha256 == $delta_four and
  .node[0].version == "2.0.0" and
  .node[0].sha256."x86_64-linux" == $epsilon_five and
  (.node[0] | has("arch") | not) and
  .node[1].version == "4.0.0" and
  .node[1].sha256.default == $eta_seven_default and
  .node[1].sha256."x86_64-linux" == $eta_seven_x64 and
  .node[1].sha256."aarch64-linux" == $eta_seven_arm64 and
  (.node[1] | has("arch") | not) and
  .native[0].version == "3.0.0" and
  .native[0].sha256."x86_64-linux" == $zeta_six_x64 and
  .native[0].sha256."aarch64-linux" == $zeta_six_arm64 and
  (.native[0] | has("arch") | not)
' "$tmpdir/grouped.json" >/dev/null
