#!/bin/sh
set -eu

project_root=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
temporary_root=$(mktemp -d "${TMPDIR:-/tmp}/product-pdf-qr-build.XXXXXX")
trap 'rm -rf "$temporary_root"' EXIT HUP INT TERM

build_once() {
  output_directory=$1
  mkdir -p "$output_directory"
  (
    cd "$project_root"
    SOURCE_DATE_EPOCH=1754006400 uv run python -m build \
      --sdist \
      --wheel \
      --outdir "$output_directory"
  )
}

build_once "$temporary_root/first"
build_once "$temporary_root/second"

first_wheel=$(find "$temporary_root/first" -name '*.whl' -type f)
second_wheel=$(find "$temporary_root/second" -name '*.whl' -type f)
first_sdist=$(find "$temporary_root/first" -name '*.tar.gz' -type f)
second_sdist=$(find "$temporary_root/second" -name '*.tar.gz' -type f)

shasum -a 256 "$first_wheel" | awk '{print $1}' >"$temporary_root/first-wheel.sha256"
shasum -a 256 "$second_wheel" | awk '{print $1}' >"$temporary_root/second-wheel.sha256"
diff -u "$temporary_root/first-wheel.sha256" "$temporary_root/second-wheel.sha256"

mkdir "$temporary_root/first-sdist" "$temporary_root/second-sdist"
tar -xzf "$first_sdist" -C "$temporary_root/first-sdist"
tar -xzf "$second_sdist" -C "$temporary_root/second-sdist"
diff -ru "$temporary_root/first-sdist" "$temporary_root/second-sdist"

sdist_manifest() {
  extracted_directory=$1
  (
    cd "$extracted_directory"
    find . -type f | LC_ALL=C sort | while IFS= read -r file_name; do
      shasum -a 256 "$file_name"
    done
  )
}

sdist_manifest "$temporary_root/first-sdist" >"$temporary_root/first-sdist.manifest"
sdist_manifest "$temporary_root/second-sdist" >"$temporary_root/second-sdist.manifest"
diff -u "$temporary_root/first-sdist.manifest" "$temporary_root/second-sdist.manifest"

echo "wheel_sha256=$(cat "$temporary_root/first-wheel.sha256")"
echo "sdist_manifest_sha256=$(shasum -a 256 "$temporary_root/first-sdist.manifest" | awk '{print $1}')"
