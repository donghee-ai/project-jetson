# project-jetson — 측정을 다시 돌리기 위한 진입점
#
# 이 저장소의 숫자는 전부 이 기기에서 잰 것이다. 아래 타깃으로 다시 뽑을 수 있다.
# 측정 환경은 environment.md 를 볼 것.

SHELL     := /bin/bash
PY        ?= python3
RESULTS   := measure/results
FIGURES   := measure/figures

.DEFAULT_GOAL := help

.PHONY: help verify bench figures clean-figures status host-status recovery-bundle links check-docs shellcheck check-fast check test hooks lock

help:  ## 이 목록
	@echo "project-jetson"
	@echo
	@awk -F':.*## ' '/^[a-z-]+:.*## /{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)
	@echo
	@echo "  측정 환경: environment.md   ·   결론: README.md"

verify:  ## JetPack·CUDA·전력모드 점검 → environment.md 를 **생성**한다
	@bash operate/tools/gen-environment.sh
	@echo
	@echo "  화면으로만 보려면: bash measure/tools/verify-jetpack.sh"

bench:  ## 모델 벤치 무인 실행 (llama-server 를 직접 띄운다 — 약 40분)
	@echo "★ 실행 중인 llama-server.service 와 :8080 을 다투므로 먼저 내릴 것:"
	@echo "    systemctl --user stop llama-server"
	@echo
	@bash measure/tools/run-model-suite.sh

figures:  ## measure/results/ 에서 그림 재생성
	@$(PY) measure/tools/plot.py --out $(FIGURES)

clean-figures:  ## 그림 삭제 (재생성 확인용)
	@rm -f $(FIGURES)/*.png

status:  ## 지금 이 기기의 현황 (서비스 · 실데이터 · 링크)
	@bash operate/tools/status.sh

# ★ status 는 **앱**, host-status 는 **기기**다. lt doctor 17항목이 전부 앱이라
#   기기가 죽어가는 것(디스크 수명 · OOM · 발열 · BSP 어긋남 · 재시작 반복)은
#   아무도 안 보고 있었다.
host-status:  ## 기기 상태 (디스크 · OOM · 발열 · BSP · 서비스 재시작 이력)
	@bash operate/tools/host-status.sh

# ★ 저장소 백업(life-trainer/scripts/backup.sh)과 다르다. 이건 **저장소 밖**까지
#   포함해 기기를 처음부터 다시 세우는 묶음이다 — openclaw 배선 · 터널 자격증명 ·
#   WiFi 드라이버(커널 hold 의 이유) · systemd 실제 상태.
recovery-bundle:  ## 기기 재구축용 묶음 하나 (★ 비밀값 포함 · 0600)
	@bash operate/tools/make-recovery-bundle.sh

# ★ 두 종류를 본다 — 마크다운 링크 + **코드 주석이 가리키는 문서와 그 § 절**.
#   후자는 2026-08-31 에 붙였다. 문서를 개명·분할했는데 코드가 안 따라간 일이
#   두 번 있었고 둘 다 몰랐다 (HISTORY/2026-08-31-the-references-did-not-follow-the-file.md).
links:  ## 문서 참조가 전부 실재하는지 (마크다운 링크 + 코드→문서 · 자기검사 포함)
	@bash operate/tools/check-links.sh --self-test
	@bash operate/tools/check-links.sh

check-docs:  ## 문서가 운영 지표를 옮겨 적고 있는지 (검사기 자기검사 포함)
	@bash operate/tools/check-docs.sh --self-test
	@bash operate/tools/check-docs.sh

# ★ pre-push 는 이걸 부른다. 6분짜리 테스트는 여기 안 넣는다 — 훅이 느리면 꺼진다.
check-fast:  ## 링크 + 문서 지표 + shellcheck (즉시. pre-push 훅이 부르는 것)
	@$(MAKE) --no-print-directory links
	@$(MAKE) --no-print-directory check-docs
	@$(MAKE) --no-print-directory shellcheck

shellcheck:  ## 셸 스크립트 정적 검사
	@# ★ 이 기기에는 apt shellcheck 이 없다. venv 의 shellcheck-py 를 쓴다.
	@if [ -x life-trainer/.venv/bin/shellcheck ]; then \
	   life-trainer/.venv/bin/shellcheck -S warning -f gcc \
	     measure/tools/*.sh operate/tools/*.sh life-trainer/scripts/*.sh life-trainer/deploy/*.sh \
	   && echo "  셸 경고 없음"; \
	 elif command -v shellcheck >/dev/null; then \
	   shellcheck -S warning -f gcc measure/tools/*.sh operate/tools/*.sh life-trainer/scripts/*.sh life-trainer/deploy/*.sh \
	   && echo "  셸 경고 없음"; \
	 else echo "  ⚠️ shellcheck 없음 — pip install shellcheck-py (CI 는 돈다)"; fi

check: check-fast  ## check-fast + 유닛 정적검사 + 테스트
	@echo
	@echo "▶ systemd 유닛 정적 검사"
	@systemd-analyze verify life-trainer/systemd/*.service life-trainer/systemd/*.timer \
	   operate/systemd/*.service operate/systemd/*.timer 2>&1 \
	   | grep -vE '^/(lib|etc)/systemd/system/' || true
	@echo "  (위에 이 저장소 유닛 관련 줄이 없으면 통과)"
	@echo
	@$(MAKE) --no-print-directory test

# ★ pyproject 는 **무엇을** 쓰는지, constraints 는 **어느 판**인지를 말한다.
#   CI 가 이 고정본으로 설치한다 — 그래야 CI 와 이 기기가 같은 판을 쓴다.
lock:  ## 의존성 버전 고정본 재생성 (life-trainer/constraints.txt)
	@bash operate/tools/lock-deps.sh

hooks:  ## pre-push 훅 설치 (git 이 훅을 안 따라가므로 명시적으로 건다)
	@install -m 755 operate/tools/pre-push.sh .git/hooks/pre-push
	@echo "  .git/hooks/pre-push 설치됨 — make check-fast 를 돌린다"


test:  ## Life Trainer 테스트 (네트워크 불필요 — conftest 가 소켓을 막는다)
	@cd life-trainer && .venv/bin/python -m pytest tests/ -q
