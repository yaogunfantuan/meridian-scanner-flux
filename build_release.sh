#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
VERSION=${1:-}
if [[ ! ${VERSION} =~ ^v[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.-]+)?$ ]]; then
  echo "用法：./build_release.sh vMAJOR.MINOR.PATCH" >&2
  exit 2
fi

PROJECT=meridian-scanner-flux
PACKAGE=${PROJECT}-${VERSION}
DIST_DIR=${SOURCE_DIR}/dist
ARCHIVE=${DIST_DIR}/${PACKAGE}.tar.gz
TEMP_DIR=$(mktemp -d)
trap 'rm -rf "${TEMP_DIR}"' EXIT
export COPYFILE_DISABLE=1

RUNTIME_FILES=(
  README.md
  install.sh
  option-scanner.env.example
  option_alert_daemon.py
  exchange_adapters.py
  option_scanner_core.py
  scanner_http.py
)

rm -rf "${DIST_DIR}"
mkdir -p "${DIST_DIR}" "${TEMP_DIR}/${PACKAGE}"
for name in "${RUNTIME_FILES[@]}"; do
  if [[ ! -f ${SOURCE_DIR}/${name} ]]; then
    echo "发布包缺少 ${name}。" >&2
    exit 1
  fi
  cp "${SOURCE_DIR}/${name}" "${TEMP_DIR}/${PACKAGE}/${name}"
done
chmod 0755 "${TEMP_DIR}/${PACKAGE}/install.sh"
printf '%s\n' "${VERSION}" >"${TEMP_DIR}/${PACKAGE}/VERSION"

tar -czf "${ARCHIVE}" -C "${TEMP_DIR}" "${PACKAGE}"
if command -v sha256sum >/dev/null 2>&1; then
  (cd "${DIST_DIR}" && sha256sum "$(basename -- "${ARCHIVE}")" >SHA256SUMS)
else
  (cd "${DIST_DIR}" && shasum -a 256 "$(basename -- "${ARCHIVE}")" >SHA256SUMS)
fi

echo "已生成："
echo "  ${ARCHIVE}"
echo "  ${DIST_DIR}/SHA256SUMS"
