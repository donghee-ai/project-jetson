# project-jetson — 측정을 다시 돌리기 위한 진입점
#
# 이 저장소의 숫자는 전부 이 기기에서 잰 것이다. 아래 타깃으로 다시 뽑을 수 있다.
# 측정 환경은 environment.md 를 볼 것.

SHELL     := /bin/bash
PY        ?= python3
RESULTS   := results
FIGURES   := figures

.DEFAULT_GOAL := help

.PHONY: help verify bench figures clean-figures status host-status links check-docs check-fast check test hooks

help:  ## 이 목록
	@echo "project-jetson"
	@echo
	@awk -F':.*## ' '/^[a-z-]+:.*## /{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)
	@echo
	@echo "  측정 환경: environment.md   ·   결론: README.md"

verify:  ## JetPack·CUDA·전력모드 점검 → environment.md 를 **생성**한다
	@bash bench/gen-environment.sh
	@echo
	@echo "  화면으로만 보려면: bash bench/verify-jetpack.sh"

bench:  ## 모델 벤치 무인 실행 (llama-server 를 직접 띄운다 — 약 40분)
	@echo "★ 실행 중인 llama-server.service 와 :8080 을 다투므로 먼저 내릴 것:"
	@echo "    systemctl --user stop llama-server"
	@echo
	@bash bench/run-model-suite.sh

figures:  ## results/ 와 benchmarks/results/ 에서 그림 재생성
	@$(PY) bench/plot.py --out $(FIGURES)

clean-figures:  ## 그림 삭제 (재생성 확인용)
	@rm -f $(FIGURES)/*.png

status:  ## 지금 이 기기의 현황 (서비스 · 실데이터 · 링크)
	@bash bench/status.sh

# ★ status 는 **앱**, host-status 는 **기기**다. lt doctor 17항목이 전부 앱이라
#   기기가 죽어가는 것(디스크 수명 · OOM · 발열 · BSP 어긋남 · 재시작 반복)은
#   아무도 안 보고 있었다.
host-status:  ## 기기 상태 (디스크 · OOM · 발열 · BSP · 서비스 재시작 이력)
	@bash bench/host-status.sh

links:  ## 문서의 상대경로 링크가 전부 실재하는지
	@bash bench/check-links.sh

check-docs:  ## 문서가 운영 지표를 옮겨 적고 있는지 (검사기 자기검사 포함)
	@bash bench/check-docs.sh --self-test
	@bash bench/check-docs.sh

# ★ pre-push 는 이걸 부른다. 6분짜리 테스트는 여기 안 넣는다 — 훅이 느리면 꺼진다.
check-fast:  ## 링크 + 문서 지표 (즉시. pre-push 훅이 부르는 것)
	@$(MAKE) --no-print-directory links
	@$(MAKE) --no-print-directory check-docs

check: check-fast  ## check-fast + 유닛 정적검사 + 테스트
	@echo
	@echo "▶ systemd 유닛 정적 검사"
	@systemd-analyze verify Life_Trainer/systemd/*.service Life_Trainer/systemd/*.timer \
	   runtime/systemd/llama-server.service 2>&1 \
	   | grep -vE '^/(lib|etc)/systemd/system/' || true
	@echo "  (위에 이 저장소 유닛 관련 줄이 없으면 통과)"
	@echo
	@$(MAKE) --no-print-directory test

hooks:  ## pre-push 훅 설치 (git 이 훅을 안 따라가므로 명시적으로 건다)
	@install -m 755 bench/pre-push.sh .git/hooks/pre-push
	@echo "  .git/hooks/pre-push 설치됨 — make check-fast 를 돌린다"


test:  ## Life Trainer 테스트 (네트워크 불필요 — conftest 가 소켓을 막는다)
	@cd Life_Trainer && .venv/bin/python -m pytest tests/ -q
