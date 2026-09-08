"""`lifetrainer.agent.sandbox` — 경로 감옥.

이 파일이 지키려는 계약:

- **허용 폴더 밖은 못 연다.** `..` · 심링크 · 절대경로 어느 쪽으로 와도 같다.
- **허용 폴더 안이어도 비밀은 못 연다.** 폴더 단위 허가의 구멍을 이름으로 메운다.
- **읽기와 쓰기가 다르다.** `config/` 는 읽히되 쓰이면 안 된다 — 분류 규칙이
  조용히 바뀌면 롤업 결과 전체가 바뀌는데 아무도 모른다.

문자열 비교로 판정하지 않는지도 본다 (`/data/agent` 허가가 `/data/agentX` 를
통과시키면 안 된다). 감옥이 한 겹뿐이라 여기가 뚫리면 아무도 안 막는다.
"""

from __future__ import annotations

import pytest

from lifetrainer.agent import sandbox as S


@pytest.fixture()
def tree(tmp_path):
    """읽기 2곳(docs·config) · 쓰기 1곳(data) 짜리 작은 나무."""
    for name in ("docs", "config", "data", "secretplace"):
        (tmp_path / name).mkdir()
    (tmp_path / "docs" / "handbook.md").write_text("본문", encoding="utf-8")
    (tmp_path / "config" / "rules.yaml").write_text("rules: []", encoding="utf-8")
    (tmp_path / "config" / "lifetrainer.toml").write_text("[slack]\nbot_token='xoxb-비밀'", encoding="utf-8")
    (tmp_path / "secretplace" / "loot.txt").write_text("훔칠 것", encoding="utf-8")
    return tmp_path


@pytest.fixture()
def sb(tree):
    return S.Sandbox.build(
        [tree / "docs", tree / "config"], [tree / "data"], base=tree
    )


# ── 기본 통과 ──────────────────────────────────────────────────────────


def test_reads_inside_read_root(sb, tree):
    assert sb.resolve_read(str(tree / "docs" / "handbook.md")) == tree / "docs" / "handbook.md"


def test_relative_path_finds_the_file_that_exists(sb, tree):
    """모델은 어느 root 인지 말해 주지 않는다. 존재하는 쪽을 골라야 한다."""
    assert sb.resolve_read("docs/handbook.md") == tree / "docs" / "handbook.md"
    assert sb.resolve_read("handbook.md") == tree / "docs" / "handbook.md"
    assert sb.resolve_read("rules.yaml") == tree / "config" / "rules.yaml"


def test_write_root_is_also_readable(sb, tree):
    """자기가 쓴 것을 못 읽으면 모델이 같은 파일을 반복해서 다시 쓴다."""
    assert tree / "data" in sb.read_roots


# ── 탈출 ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "escape",
    [
        "../secretplace/loot.txt",
        "docs/../../secretplace/loot.txt",
        "/etc/passwd",
        "docs/../../../../etc/hosts",
    ],
)
def test_escapes_are_refused(sb, escape):
    with pytest.raises(S.SandboxError):
        sb.resolve_read(escape)


def test_symlink_pointing_outside_is_refused(sb, tree):
    """realpath 로 접은 뒤에 판정해야 한다 — 문자열만 보면 통과한다."""
    (tree / "docs" / "escape").symlink_to(tree / "secretplace")
    with pytest.raises(S.SandboxError):
        sb.resolve_read(str(tree / "docs" / "escape" / "loot.txt"))


def test_prefix_is_not_containment(tree):
    """`/data` 허가가 `/dataX` 를 통과시키면 안 된다 (startswith 로 짠 감옥의 구멍)."""
    (tree / "dataX").mkdir()
    (tree / "dataX" / "loot.txt").write_text("x", encoding="utf-8")
    sb = S.Sandbox.build([tree / "data"], [tree / "data"], base=tree)
    with pytest.raises(S.SandboxError):
        sb.resolve_read(str(tree / "dataX" / "loot.txt"))


# ── 비밀 ──────────────────────────────────────────────────────────────


def test_secret_file_inside_allowed_folder_is_refused(sb):
    """`config/` 는 허용이지만 `lifetrainer.toml` 은 Slack 토큰과 검색 키를 갖고 있다."""
    with pytest.raises(S.SandboxError):
        sb.resolve_read("config/lifetrainer.toml")


@pytest.mark.parametrize(
    "name",
    [
        "lifetrainer.toml.bak-before-ingest",  # 백업본이 같은 비밀을 갖고 있다
        "openclaw.json",
        "websecret",
        "ingestsecret",
        "lifetrainer.db",
        ".env",
    ],
)
def test_secret_names_are_refused_anywhere(sb, tree, name):
    (tree / "docs" / name).write_text("비밀", encoding="utf-8")
    with pytest.raises(S.SandboxError):
        sb.resolve_read(f"docs/{name}")


def test_secret_files_are_hidden_from_listings(sb, tree):
    """이름만으로도 힌트가 된다 — 목록에서도 감춘다."""
    (tree / "docs" / "websecret").write_text("비밀", encoding="utf-8")
    listing = S.list_dir(sb, "docs")
    assert "handbook.md" in listing
    assert "websecret" not in listing


# ── 쓰기 ──────────────────────────────────────────────────────────────


def test_write_outside_write_root_is_refused(sb):
    with pytest.raises(S.SandboxError):
        sb.resolve_write("docs/evil.md")


def test_relative_write_lands_in_the_write_root(sb, tree):
    assert sb.resolve_write("notes/today.md") == tree / "data" / "notes" / "today.md"


def test_write_creates_parents_inside_the_jail(sb, tree):
    S.write_text(sb, "notes/today.md", "메모")
    assert (tree / "data" / "notes" / "today.md").read_text(encoding="utf-8") == "메모"


def test_write_cannot_create_parents_outside(sb, tree):
    with pytest.raises(S.SandboxError):
        S.write_text(sb, str(tree / "secretplace" / "new" / "x.md"), "x")
    assert not (tree / "secretplace" / "new").exists()


# ── 읽은 양 ────────────────────────────────────────────────────────────


def test_long_file_is_truncated_and_says_so(sb, tree):
    """자른 것을 말하지 않으면 모델이 뒷부분을 기억으로 채운다."""
    (tree / "docs" / "big.md").write_text("가" * 5000, encoding="utf-8")
    out = S.read_text(sb, "docs/big.md", max_bytes=100)
    assert "100바이트만 실었습니다" in out
    assert len(out) < 5000


def test_a_huge_file_is_not_loaded_whole(sb, tree, monkeypatch):
    """`read_bytes()` 는 자르기 전에 파일 전체를 메모리에 올린다.

    상주 300MB 예산에서 감옥 안의 큰 파일 하나가 그걸 넘길 수 있다 —
    `data/agent/` 는 에이전트가 쓰는 곳이라 커질 수 있다.
    """
    big = tree / "docs" / "huge.md"
    big.write_text("가" * 20000, encoding="utf-8")

    def explode(self):  # pragma: no cover - 불리면 실패다
        raise AssertionError("파일을 통째로 읽으면 안 된다")

    monkeypatch.setattr(type(big), "read_bytes", explode)
    out = S.read_text(sb, "docs/huge.md", max_bytes=200)
    assert "200바이트만 실었습니다" in out


def test_a_broken_symlink_does_not_kill_the_listing(sb, tree):
    """항목 하나 때문에 폴더 전체를 못 보면 안 된다."""
    (tree / "docs" / "dangling").symlink_to(tree / "docs" / "gone.md")
    listing = S.list_dir(sb, "docs")
    assert "handbook.md" in listing
    assert "dangling" in listing


def test_missing_file_says_so(sb):
    with pytest.raises(S.SandboxError, match="파일이 없습니다"):
        S.read_text(sb, "docs/nope.md")


def test_directory_read_points_at_list_dir(sb):
    with pytest.raises(S.SandboxError, match="list_dir"):
        S.read_text(sb, "docs")


# ── 안전한 방향으로 실패한다 ─────────────────────────────────────────────


def test_missing_roots_are_dropped_not_widened(tmp_path):
    """설정 오타가 경계를 **넓히는** 방향으로 실패하면 안 된다."""
    sb = S.Sandbox.build([tmp_path / "nope"], [tmp_path / "also-nope"], base=tmp_path)
    assert sb.read_roots == ()
    with pytest.raises(S.SandboxError):
        sb.resolve_read(str(tmp_path / "anything.md"))


def test_empty_and_null_paths_are_refused(sb):
    for bad in ("", "   ", "a\x00b"):
        with pytest.raises(S.SandboxError):
            sb.resolve_read(bad)


def test_describe_uses_short_paths(sb):
    """거부 한 번이 100토큰이면 그 글자가 전부 다음 턴의 입력이 된다."""
    text = sb.describe()
    assert "docs" in text and str(sb.read_roots[0]) not in text
