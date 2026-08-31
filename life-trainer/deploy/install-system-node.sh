#!/usr/bin/env bash
# 시스템 Node 설치 — /usr/local 에 공식 tarball 을 푼다. (root 권한 필요)
#
# 왜 필요한가:
#   openclaw daemon install 이 만든 유닛은 nvm 경로의 node 를 직접 가리킨다.
#   지금은 동작하지만 nvm 에서 그 버전을 지우거나 바꾸면 서비스가 깨진다.
#   OpenClaw 의 service-audit 이 "시스템 Node" 로 인정하는 경로는 Linux 기준
#   /usr/local/bin/node 와 /usr/bin/node 두 개뿐이고(dist/runtime-paths-*.js
#   buildSystemNodeCandidates), 경로가 /.nvm/ · /.fnm/ · /.volta/ 등을 거치면
#   version-manager 로 판정된다. 그래서 /usr/local 에 직접 깐다.
#
#   Ubuntu 22.04 의 apt nodejs 는 12.x 라 쓸 수 없다 (OpenClaw 는 22.22.3+ 또는
#   24.15+ 를 요구한다 — node:sqlite 와 WAL-reset-safe SQLite 가 필요하다).
#
# 사용법:
#   sudo bash openclaw-setup/install-system-node.sh
set -euo pipefail

NODE_VERSION="${NODE_VERSION:-v24.19.0}"   # 24.15+ 필요. 24.19.0 = LTS Krypton
ARCH="linux-arm64"                          # Jetson Orin NX = aarch64
TARBALL="node-${NODE_VERSION}-${ARCH}.tar.xz"
BASE_URL="https://nodejs.org/dist/${NODE_VERSION}"
PREFIX="/usr/local"

if [ "$(id -u)" -ne 0 ]; then
  echo "root 권한이 필요하다: sudo bash $0" >&2
  exit 1
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
cd "$WORK"

echo "==> 내려받는 중: ${TARBALL}"
curl -sSL --fail -o "$TARBALL" "${BASE_URL}/${TARBALL}"
curl -sSL --fail -o SHASUMS256.txt "${BASE_URL}/SHASUMS256.txt"

echo "==> 체크섬 검증"
grep " ${TARBALL}\$" SHASUMS256.txt | sha256sum -c -

echo "==> ${PREFIX} 에 설치"
# --strip-components=1 로 node-vX-linux-arm64/{bin,lib,include,share} 를
# /usr/local 아래에 그대로 병합한다. CHANGELOG 류는 뺀다.
tar -xJf "$TARBALL" -C "$PREFIX" --strip-components=1 \
  --exclude=CHANGELOG.md --exclude=LICENSE --exclude=README.md

echo "==> 확인"
"${PREFIX}/bin/node" --version
"${PREFIX}/bin/npm" --version

echo
echo "완료. 이어서 사용자 계정으로 실행:"
echo "  bash openclaw-setup/use-system-node.sh"
