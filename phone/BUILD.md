# LT Phone (aw-android 포크) — 빌드 환경

작업 저장소: `c:\jetsonapk\aw-android` · 브랜치 `lifetrainer` · 빌드 호스트 **WSL2 Ubuntu 24.04**

## 왜 WSL 인가

aw-android 는 `Makefile` + bash 스크립트(`compile-android.sh`, `uname`/`cut` 사용)로
`.so` 를 만든다. 업스트림 CI 도 전부 Ubuntu 에서 돈다. Git Bash 에는 `make` 가 없다.

## 툴체인 (전부 `$HOME`, sudo 불필요)

| | 버전 | 위치 |
|---|---|---|
| JDK | 17.0.20 (Temurin) | `~/opt/jdk17` |
| Node | 20.20.2 | `~/opt/node` |
| Rust | 1.98.0 **stable** (nightly 아님) | `~/.cargo` |
| Android SDK | platform 36, build-tools 36.0.0 | `~/Android/Sdk` |
| NDK | **28.2.13676358** (r28c) | `~/Android/Sdk/ndk/...` |

환경변수는 `~/.lt-build-env` 에 모아 뒀다. **모든 빌드는 이걸 먼저 source 한다.**

## 빌드

```bash
wsl -d Ubuntu
. ~/.lt-build-env
cd "$LT_REPO"                     # /mnt/c/jetsonapk/aw-android

make aw-server-rust               # webui + rust .so  (RELEASE=true 면 release)
python3 scripts/check-jnilibs.py  # ELF·16KB 정렬 검증
./gradlew assembleDebug           # 또는 assembleRelease
```

> `./gradlew` 단독으로는 안 된다. rust-android-gradle 플러그인은 `openssl-sys` 때문에
> 동작하지 않고(build.gradle 주석에 명시), `.so` 는 **Makefile 이** 만들어
> `mobile/src/main/jniLibs/<abi>/` 로 하드 링크한다. **`make` 가 진입점이다.**

## ★ 빌드 산출물이 프로젝트 폴더에 없는 이유

`/mnt/c` 는 9p 파일시스템이라 Gradle/Cargo 처럼 작은 파일 IO 가 많은 빌드가 몇 배 느리다.
**변동이 심한 중간산출물만** WSL 네이티브 FS 로 심볼릭 링크했다:

| 링크 | 실제 위치 | 크기 |
|---|---|---|
| `aw-server-rust/target` | `~/.lt-build/cargo-target` | 15 GB |
| `aw-server-rust/aw-webui/node_modules` | `~/.lt-build/webui/node_modules` | 602 MB |
| `mobile/src/main/jniLibs` | `~/.lt-build/jniLibs` | — |

**소스·`.git`·APK 는 프로젝트 폴더에 그대로 있다.**

## 겪은 함정 (재현될 것들)

**1. `mode 100644 => 100755` 로 183개 파일이 modified 로 뜬다**
DrvFs 가 `/mnt/c` 의 모든 파일을 실행 가능으로 마운트해서 생긴다. 내용 변경이 아니다.
```bash
git config core.fileMode false     # 서브모듈에도 적용
```

**2. `Cannot find module '@vue/cli-shared-utils'`**
node_modules 를 심볼릭 링크할 때 **실제 경로의 마지막 요소가 반드시 `node_modules`** 여야
한다. Node 는 심볼릭 링크를 실제 경로로 해석한 뒤 상위로 올라가며 그 이름을 찾기 때문에,
`webui-node_modules` 같은 이름으로 두면 탐색이 끊긴다.

**3. `ln: failed to create hard link: Invalid cross-device link`**
Makefile 이 `target/` → `jniLibs/` 로 **하드 링크**를 건다. 둘이 다른 파일시스템에 있으면
실패한다. 그래서 위 표에서 `jniLibs` 도 같이 네이티브 FS 로 뺐다.

**4. `install-ndk.sh` 가 NDK r25c 를 따로 받으려 한다**
`ANDROID_NDK_HOME` 이 설정돼 있으면 건너뛴다. `~/.lt-build-env` 가 이미 설정한다.

## 서명

keystore 는 `c:\jetsonapk\keys\` 에 백업돼 있다 (README 참조).
gradle 은 `~/.gradle/gradle.properties` 의 `LT_*` 속성을 읽는다 — **저장소에 넣지 않는다.**
속성이 없으면 release 빌드는 그냥 서명만 안 된 채 진행된다(빌드는 깨지지 않는다).
