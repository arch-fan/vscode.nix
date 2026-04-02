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

cp "$fixtures_dir/extensions-flat.json" "$tmpdir/flat.json"
cp "$fixtures_dir/extensions-flat.json" "$tmpdir/flat.original.json"
cp "$fixtures_dir/extensions-grouped.json" "$tmpdir/grouped.json"
cp "$fixtures_dir/extensions-grouped.json" "$tmpdir/grouped.original.json"

mkdir -p "$tmpdir/bin"
bash_path="$(command -v bash)"
{
  printf '#!%s\n' "$bash_path"
  cat <<'EOF'
set -euo pipefail

if [[ "${1:-}" != "store" || "${2:-}" != "prefetch-file" ]]; then
  echo "unexpected nix invocation: $*" >&2
  exit 1
fi

url="${@: -1}"
case "$url" in
  *"/alpha/extension/one/2.0.0/"*)
    hash="sha256-alpha-one-2.0.0"
    ;;
  *"/beta/extension/two/2.0.0-beta.1/"*)
    hash="sha256-beta-two-2.0.0-beta.1"
    ;;
  *"/delta/extension/four/1.2.0/"*)
    hash="sha256-delta-four-1.2.0"
    ;;
  *"/epsilon/extension/five/2.0.0/"*"?targetPlatform=linux-x64")
    hash="sha256-epsilon-five-2.0.0-linux-x64"
    ;;
  *"/zeta/extension/six/3.0.0/"*"?targetPlatform=linux-x64")
    hash="sha256-zeta-six-3.0.0-linux-x64"
    ;;
  *"/zeta/extension/six/3.0.0/"*"?targetPlatform=linux-arm64")
    hash="sha256-zeta-six-3.0.0-linux-arm64"
    ;;
  *)
    echo "unexpected prefetch url: $url" >&2
    exit 1
    ;;
esac

printf '{"hash":"%s"}\n' "$hash"
EOF
} > "$tmpdir/bin/nix"
chmod +x "$tmpdir/bin/nix"

port_file="$tmpdir/port"
python "$server_script" "$fixtures_dir/marketplace-responses.json" "$port_file" "$fixtures_dir/vsix-platforms.json" &
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

export PATH="$tmpdir/bin:$PATH"
export VSCODE_MARKETPLACE_URL="http://127.0.0.1:$(cat "$port_file")"
export VSCODE_GALLERY_BASE_URL="http://127.0.0.1:$(cat "$port_file")"

if python "$script" --check "$tmpdir/flat.json"; then
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

python "$script" --jobs 2 "$tmpdir/flat.json"
jq -e '
  length == 2 and
  .[0].publisher == "alpha" and
  .[0].name == "one" and
  .[0].version == "2.0.0" and
  .[0].sha256 == "sha256-alpha-one-2.0.0" and
  .[1].publisher == "beta" and
  .[1].name == "two" and
  .[1].version == "2.0.0-beta.1" and
  .[1].sha256 == "sha256-beta-two-2.0.0-beta.1" and
  .[1].prerelease == true
' "$tmpdir/flat.json" >/dev/null

python "$script" --group node "$tmpdir/grouped.json"
jq -e '
  .base[0].version == "1.0.0" and
  .base[0].sha256 == "sha256-gamma-three-1.0.0" and
  .base[1].version == "1.0.0" and
  .base[1].sha256 == "sha256-delta-four-1.0.0" and
  .node[0].version == "2.0.0" and
  .node[0].sha256 == "sha256-epsilon-five-2.0.0-linux-x64" and
  .node[0].arch == "linux-x64"
' "$tmpdir/grouped.json" >/dev/null

python "$script" --include-prerelease "$tmpdir/grouped.json"
jq -e '
  .base[0].version == "1.0.0" and
  .base[0].sha256 == "sha256-gamma-three-1.0.0" and
  .base[0].prerelease == false and
  .base[1].version == "1.2.0" and
  .base[1].sha256 == "sha256-delta-four-1.2.0" and
  .node[0].version == "2.0.0" and
  .node[0].sha256 == "sha256-epsilon-five-2.0.0-linux-x64" and
  .native[0].version == "3.0.0" and
  .native[0].sha256."x86_64-linux" == "sha256-zeta-six-3.0.0-linux-x64" and
  .native[0].sha256."aarch64-linux" == "sha256-zeta-six-3.0.0-linux-arm64"
' "$tmpdir/grouped.json" >/dev/null
