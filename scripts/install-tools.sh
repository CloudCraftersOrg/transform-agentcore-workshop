#!/usr/bin/env bash
# Download pinned kind and kubeconform into ./.tools/bin (gitignored), verifying SHA-256 checksums.
# Idempotent: tools already at the pinned version are left alone.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BIN="$ROOT/.tools/bin"
KIND_VERSION="v0.33.0"
KUBECONFORM_VERSION="v0.8.0"

os="$(uname -s | tr '[:upper:]' '[:lower:]')"
case "$(uname -m)" in
  arm64|aarch64) arch="arm64" ;;
  x86_64|amd64) arch="amd64" ;;
  *) echo "unsupported architecture: $(uname -m)" >&2; exit 1 ;;
esac
case "$os" in darwin|linux) ;; *) echo "unsupported OS: $os" >&2; exit 1 ;; esac
platform="$os-$arch"

# Checksums copied from the upstream release pages (kind-*.sha256sum, kubeconform CHECKSUMS).
kind_sha() {
  case "$1" in
    darwin-arm64) echo 0c8c7dbe5e23594a198b786c4bc13dacc101fa6196b0cb0b23a1ca44e61f4b4f ;;
    darwin-amd64) echo 5a99f26f57246dc9319dd294803313197a0f34d33c525b3ea8b655db5916ece0 ;;
    linux-arm64) echo 20022bee6cfcd5086cb7234d218e3454e6090022f2a8f55d1fa7fcf42c3867a2 ;;
    linux-amd64) echo aee6151561422756b764a4ae28e7f44cda5af5a9eead3cc9985112b1de8d8e0d ;;
  esac
}
kubeconform_sha() {
  case "$1" in
    darwin-arm64) echo f84f4dfbebf4a6b0b230385fa065a39ea35e02608c2b50d025dcf64775a69d67 ;;
    darwin-amd64) echo 71dbc87ac9f24099a62b93570e65aa06312ba6ac8aea63b7f86e9d999edf5a92 ;;
    linux-arm64) echo 1f53fc8e81258197a35e8603054162a5af1de8c5af13746c71ab680d9534ed87 ;;
    linux-amd64) echo 9bc2bffbf71f261128533edaf912153948b7ff238f9a531ae6d34466ec287883 ;;
  esac
}

sha256() { if command -v sha256sum >/dev/null; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi; }

fetch() {  # fetch <url> <dest> <expected-sha256>
  local tmp; tmp="$(mktemp)"
  curl -fsSL --retry 3 --max-time 120 -o "$tmp" "$1"
  local got; got="$(sha256 "$tmp")"
  if [[ "$got" != "$3" ]]; then rm -f "$tmp"; echo "checksum mismatch for $1 (got $got)" >&2; exit 1; fi
  mv "$tmp" "$2"
}

mkdir -p "$BIN"

if [[ -x "$BIN/kind" ]] && "$BIN/kind" version 2>/dev/null | grep -q "$KIND_VERSION"; then
  echo "kind $KIND_VERSION already installed"
else
  echo "installing kind $KIND_VERSION ($platform)"
  fetch "https://github.com/kubernetes-sigs/kind/releases/download/$KIND_VERSION/kind-$platform" "$BIN/kind" "$(kind_sha "$platform")"
  chmod +x "$BIN/kind"
fi

if [[ -x "$BIN/kubeconform" ]] && "$BIN/kubeconform" -v 2>/dev/null | grep -q "${KUBECONFORM_VERSION#v}"; then
  echo "kubeconform $KUBECONFORM_VERSION already installed"
else
  echo "installing kubeconform $KUBECONFORM_VERSION ($platform)"
  work="$(mktemp -d)"
  fetch "https://github.com/yannh/kubeconform/releases/download/$KUBECONFORM_VERSION/kubeconform-$platform.tar.gz" \
    "$work/kubeconform.tar.gz" "$(kubeconform_sha "$platform")"
  tar -xzf "$work/kubeconform.tar.gz" -C "$work" kubeconform
  mv "$work/kubeconform" "$BIN/kubeconform"
  chmod +x "$BIN/kubeconform"
  rm -rf "$work"
fi

"$BIN/kind" version
echo "kubeconform $("$BIN/kubeconform" -v)"
echo "tools ready in $BIN (add it to PATH or let the agents find it automatically)"
