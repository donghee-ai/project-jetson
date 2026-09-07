#!/usr/bin/env bash
# 에이전트 대화 세션 파일을 **일주일**만 남긴다 (docs/issues/0011).
#
# ★ 왜 용량이 아니라 기간인가
#   27MB 다. 이 기기의 병목은 디스크가 아니라 메모리라서 용량은 판단 기준이 아니다.
#   이 파일들에는 **대화 원문**이 들어 있다 — 그래서 기준은 보존 기간이고,
#   사람이 "일주일 단위 삭제" 로 정했다 (2026-09-07).
#
# ★ 왜 지우기 전에 세는가
#   지운 뒤에 "몇 개 지웠지?" 를 물으면 답할 방법이 없다. 되돌릴 수 없는 작업은
#   무엇을 없앴는지 먼저 말한다 (저장소 규칙 §5).
#
# ★ 왜 `--dry-run` 이 기본이 아닌가
#   이건 타이머가 부르는 청소다. 기본이 미리보기면 타이머가 아무것도 안 한다 —
#   *만들었다 ≠ 그게 일을 했다*. 대신 무엇을 지웠는지 journal 에 남긴다.
#   손으로 볼 때는 `--dry-run` 을 준다.
set -euo pipefail

KEEP_DAYS=${LT_SESSION_KEEP_DAYS:-7}
DRY=""
[ "${1:-}" = "--dry-run" ] && DRY=1

removed_total=0
for dir in "$HOME"/.openclaw/agents/*/sessions; do
  [ -d "$dir" ] || continue
  agent=$(basename "$(dirname "$dir")")
  before=$(find "$dir" -type f | wc -l)

  # ★ `-mtime +N` 은 **N일을 꽉 채워 넘은 것**만 고른다. 오늘 만든 것은 절대 안 걸린다.
  count=$(find "$dir" -type f -mtime +"$KEEP_DAYS" | wc -l)
  if [ "$count" -eq 0 ]; then
    echo "  $agent: ${before}개 · ${KEEP_DAYS}일 넘은 것 없음"
    continue
  fi
  size=$(find "$dir" -type f -mtime +"$KEEP_DAYS" -printf '%s\n' | awk '{s+=$1} END {printf "%.1fMB", s/1048576}')

  if [ -n "$DRY" ]; then
    echo "  $agent: ${before}개 중 ${count}개(${size}) 를 지울 것 — 미리보기"
  else
    find "$dir" -type f -mtime +"$KEEP_DAYS" -delete
    echo "  $agent: ${before}개 → $(find "$dir" -type f | wc -l)개 (${count}개 ${size} 삭제)"
    removed_total=$((removed_total + count))
  fi
done

[ -z "$DRY" ] && echo "  ${KEEP_DAYS}일보다 오래된 세션 ${removed_total}개 삭제"
exit 0
