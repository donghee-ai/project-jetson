# project-jetson — 측정을 다시 돌리기 위한 진입점
#
# 이 저장소의 숫자는 전부 이 기기에서 잰 것이다. 아래 타깃으로 다시 뽑을 수 있다.
# 측정 환경은 environment.md 를 볼 것.

SHELL     := /bin/bash
PY        ?= python3
RESULTS   := results
FIGURES   := figures

.DEFAULT_GOAL := help

.PHONY: help verify bench figures clean-figures status links test

help:  ## 이 목록
	@echo "project-jetson"
	@echo
	@awk -F':.*## ' '/^[a-z-]+:.*## /{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)
	@echo
	@echo "  측정 환경: environment.md   ·   결론: README.md"

verify:  ## JetPack·CUDA·전력모드 점검 → environment.md 갱신
	@bash bench/verify-jetpack.sh

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

links:  ## 문서의 상대경로 링크가 전부 실재하는지
	@bash bench/check-links.sh

test:  ## Life Trainer 테스트 (네트워크 불필요 — conftest 가 소켓을 막는다)
	@cd Life_Trainer && .venv/bin/python -m pytest tests/ -q
