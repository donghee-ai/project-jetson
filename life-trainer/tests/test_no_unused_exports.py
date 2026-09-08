"""프로덕션에서 한 번도 안 불리는 공개 함수를 잡는다.

## 왜 이 검사가 생겼나

2026-09-01, `migrate()` 에 **프로덕션 호출자가 하나도 없다는 것**을 라이브가 깨지고서야
알았다. 마이그레이션 006 이 컬럼을 더했는데 그 마이그레이션이 돌지 않아 10분마다 도는
롤업이 통째로 실패했다. 테스트는 전부 초록불이었다 — **테스트가 `migrate()` 를 직접
불렀기 때문이다.**

그래서 훑어보니 한 건이 아니었다. `purge_done` 은 호출자가 없는데 `lt doctor` 의 주석이
*"14일 뒤에 걷어가므로 done 은 회전한다"* 를 전제로 판정 근거를 쓰고 있었다.
실측하니 가장 오래된 done 이 17일 전이었다 — **주석이 코드보다 낙관적이었다.**

## 언제 안 우나 (CLAUDE.md §1)

- 함수를 **부르면** 꺼진다 (`purge_done` 이 그렇게 꺼졌다)
- 함수를 **지우면** 꺼진다
- **이유를 적어 목록에 넣으면** 꺼진다 ← 이게 이 검사의 핵심이다

지우라는 검사가 아니다. **"안 불리는데 왜 남아 있나"에 글로 답하게 하는** 검사다.
답이 적혀 있으면 다음 사람이 실측을 다시 안 해도 된다.

## 무엇을 안 세나

| | 왜 |
|---|---|
| `_` 로 시작 | 비공개. 모듈 안에서만 쓴다 |
| 데코레이터가 붙은 것 | `@app.get(...)` 같은 **등록이 곧 호출자**다 |
| 기반 클래스가 있는 클래스의 메서드 | `HTMLParser.handle_data` 처럼 **부모가 부른다** |
| 주석·독스트링의 언급 | AST 로 세므로 애초에 안 잡힌다 — `purge_done` 이 이래서 숨어 있었다 |

## 한계 — 이름이 같으면 가려진다

판정 단위가 **이름**이지 바인딩이 아니다. 어딘가에 같은 이름의 **속성**이 있으면
함수가 불리는 것처럼 보인다.

실제로 걸렸다: `carry_debt()` 는 여전히 호출자가 없는데, `DailyStats.carry_debt`
필드를 만들자 `stats.carry_debt` 가 `ast.Attribute(attr="carry_debt")` 로 잡혀
**검사가 조용해졌다.** 필드를 `repeated_defers` 로 바꿔 충돌을 없앴다.

정확히 하려면 타입 해석이 필요한데 그건 이 검사의 값어치를 넘는다.
**이름을 겹치지 않게 짓는 쪽이 싸다.**
"""

from __future__ import annotations

import ast
import pathlib

PKG = pathlib.Path(__file__).resolve().parent.parent / "lifetrainer"


# ── 안 불리는데 남겨 둔 것 — **한 줄 이유가 의무다** ─────────────────────
#
# 새로 여기 넣을 때: "쓸 데가 있어서"는 이유가 아니다. *어느 경로가 대신 쓰이는지*,
# 또는 *무엇이 미완이라 안 불리는지*를 적는다.
ALLOWED: dict[str, str] = {
    "assert_no_pattern": "테스트 헬퍼가 프로덕션 모듈(agent/catalog.py)에 있다 — tests/ 로 옮길 것",
    "carry_debt": (
        "뷰를 그대로 주는 저수준 판. 사람이 읽는 경로는 `carry_debt_titles` 이고"
        " 그쪽이 일일 리포트에 붙어 있다(2026-09-01). 이건 id·깊이만 필요할 때의 원본"
    ),
    "clear_override": "`clear_override_range(day, s, s+1)` 로 표현된다. 웹·CLI 는 range 판만 쓴다",
    "set_override": "`set_override_range(day, s, s+1, cat)` 로 표현된다. 웹·CLI 는 range 판만 쓴다",
    # subject — **없는 것은 쓰는 길뿐이다.** 지우자고 했다가 되돌렸다 (2026-09-03):
    #   읽는 쪽(`list_subjects`)은 슬랙이 이미 쓰고 있고, 그 기능의 테스트가
    #   `create_subject` 를 픽스처로 쓴다. 지우면 그 테스트가 raw SQL 로 내려앉는다.
    #   모듈은 온전하고 전용 테스트 11개가 지키는 중이다 — 빠진 것은 CLI·슬랙 진입점이다.
    "create_subject": "쓰는 길(CLI·슬랙 명령)만 없다. 읽는 쪽은 슬랙이 쓰고 전용 테스트 11개가 있다",
    "update_subject": "위와 같음 — 진입점이 없을 뿐 모듈은 온전하다",
    "delete_subject": "위와 같음 — 진입점이 없을 뿐 모듈은 온전하다",
    "palette_choices": "subject 색상 선택기가 쓸 목록. 그 선택기(진입점)가 아직 없다",
    "list_plans": "`plan.achieve.plans_for_day` 가 실경로다. 이쪽은 그날의 인스턴스를 안 만든다",
    "variant_names": "팔레트 고르개를 없애면서(2026-09-01) 남은 잔여물. 변주는 PNG 쪽만 쓴다",
    "verify_link_token": (
        "★ 함정이라 주의. `/auth/enter` 는 `consume_link_token` 을 쓴다 — 이쪽은"
        " **1회용 검사를 안 한다.** 실수로 이걸 쓰면 토큰 재사용이 열린다"
    ),
}


def _unused_public_functions(root: pathlib.Path) -> dict[str, str]:
    """{함수명: 정의 위치} — 프로덕션 어디에서도 이름이 안 나오는 공개 함수."""
    defined: dict[str, str] = {}
    referenced: set[str] = set()

    class Collect(ast.NodeVisitor):
        def __init__(self, path: pathlib.Path) -> None:
            self.path = path
            self.in_subclass: list[bool] = []

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self.in_subclass.append(bool(node.bases))
            self.generic_visit(node)
            self.in_subclass.pop()

        def _function(self, node) -> None:
            skip = (
                node.name.startswith("_")
                or node.decorator_list                       # 등록이 곧 호출자
                or (self.in_subclass and self.in_subclass[-1])  # 부모가 부른다
            )
            if not skip:
                defined.setdefault(node.name, f"{node.name} ({self.path.name}:{node.lineno})")
            self.generic_visit(node)

        visit_FunctionDef = _function       # type: ignore[assignment]
        visit_AsyncFunctionDef = _function  # type: ignore[assignment]

    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        Collect(path).visit(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                referenced.add(node.id)
            elif isinstance(node, ast.Attribute):
                referenced.add(node.attr)
            elif isinstance(node, ast.alias):
                referenced.add((node.asname or node.name).split(".")[-1])

    return {name: loc for name, loc in defined.items() if name not in referenced}


def test_안_불리는_공개_함수는_이유가_적혀_있다():
    unused = _unused_public_functions(PKG)
    surprises = {n: loc for n, loc in unused.items() if n not in ALLOWED}
    assert not surprises, (
        "프로덕션에서 한 번도 안 불리는 공개 함수가 새로 생겼다:\n  "
        + "\n  ".join(sorted(surprises.values()))
        + "\n\n부르거나, 지우거나, ALLOWED 에 **이유와 함께** 넣어라."
    )


def test_목록이_묵은_것을_안_들고_있다():
    """고쳐 놓고 목록에서 안 뺀 것을 잡는다.

    ★ 이게 없으면 목록이 한 방향으로만 자란다. `purge_done` 을 부르게 만든 뒤에도
      목록에 남아 있으면, 다음 사람은 그게 아직 안 불린다고 읽는다.
    """
    unused = _unused_public_functions(PKG)
    stale = sorted(set(ALLOWED) - set(unused))
    assert not stale, f"이제 불리는데 목록에 남아 있다 — 지워라: {stale}"


# ── 검사기 자기 시험 ─────────────────────────────────────────────────────
#
# ★ 검사기를 믿지 말고 재 본다. 규칙만 있고 검사가 없어서 네 번 실패한 저장소다.


def test_검출기_자기시험(tmp_path):
    (tmp_path / "m.py").write_text(
        '''
def called_one():
    return 1

def caller():
    return called_one()

def never_called():          # ← 이것만 잡혀야 한다
    return 2

def _private_unused():
    return 3

class Base:
    pass

class Child(Base):
    def handle_thing(self):  # 부모가 부른다 — 안 잡는다
        return 4

def decorated_is_registered():
    return 5
decorated_is_registered = staticmethod(decorated_is_registered)  # noqa

# purge_done 이 숨었던 방식: 주석에만 이름이 있으면 안 불리는 것이다.
# never_called() 라고 주석에 적어도 소용없어야 한다.
''',
        encoding="utf-8",
    )
    (tmp_path / "n.py").write_text(
        "from m import called_one\n\n"
        "@some_decorator\n"
        "def route_registered_by_decorator():\n    return called_one()\n\n"
        "def some_decorator(f):\n    return f\n",
        encoding="utf-8",
    )

    found = _unused_public_functions(tmp_path)
    assert "never_called" in found, "안 불리는 함수를 놓쳤다"
    assert "called_one" not in found, "불리는 함수를 잡았다 (오탐)"
    assert "_private_unused" not in found, "비공개를 잡았다 (오탐)"
    assert "handle_thing" not in found, "기반 클래스의 오버라이드를 잡았다 (오탐)"
    assert "route_registered_by_decorator" not in found, "데코레이터 등록을 잡았다 (오탐)"
