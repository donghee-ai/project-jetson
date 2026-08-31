"""검사기가 **반드시 잡아야 하는** 예제 (파이썬).

이 파일은 `operate/tools/check-links.sh --self-test` 만 읽는다. 평소 검사에서는 제외된다 —
검사기가 자기 시험지에 걸려 넘어지면 안 되기 때문이다.

여기 있는 세 줄이 전부 통과하기 시작하면, 그건 검사기가 헐거워졌다는 뜻이다.
2026-08-28 에 실제로 이 부류 28곳이 아무도 모르게 죽어 있었다.
"""

# ① 폴더가 붙었는데 그런 문서가 없다 — 개명·삭제된 문서를 가리키는 전형이다.
#    docs/there-is-no-such-document.md
#
# ② 절 번호가 붙었는데 문서 자체가 없다 — `openclaw-setup` 개명 때 난 일이다.
#    openclaw-agent.md §3
#
# ③ 문서는 살아 있는데 그 절이 없다 — known-issues 를 issues/ 로 쪼갤 때 난 일이다.
#    파일 존재만 보는 검사는 이걸 못 잡는다.
#    operate/notes/agent-gateway.md §99
