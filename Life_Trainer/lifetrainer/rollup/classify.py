"""규칙 기반 활동 분류기 (계약서 §4).

`config/rules.yaml` 을 읽어 (app, title, url) 조합을 카테고리로 분류한다.
규칙은 리스트 순서대로 평가되고 첫 매칭이 승리한다 — 그래서 순서가 곧 우선순위다.
매칭 실패분은 `default_category` 로 떨어지고, 그 (app, title) 지문은
`rollup.py` 가 `unclassified` 테이블에 누적한다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

# away/off 는 rollup 이, unknown 은 default_category 가 채우는 예약 카테고리다.
# 규칙 파일에서 이 셋을 직접 지정하면 설정 오류로 본다.
_RESERVED_CATEGORIES = frozenset({"away", "off", "unknown"})


@dataclass(frozen=True)
class Category:
    id: str
    label: str
    color: str


@dataclass(frozen=True)
class Rule:
    index: int
    category: str
    subcategory: str | None
    app: re.Pattern | None
    title: re.Pattern | None
    url: re.Pattern | None

    def matches(self, app: str | None, title: str | None, url: str | None) -> bool:
        """세 필드는 AND. 규칙에 지정된 필드만 검사하고, 그 필드의 값이 없으면(None) 불일치로 본다."""
        if self.app is None and self.title is None and self.url is None:
            # match 가 비어 있는 규칙은 만들어지지 않지만 방어적으로 처리한다.
            return False
        if self.app is not None:
            if app is None or not self.app.search(app):
                return False
        if self.title is not None:
            if title is None or not self.title.search(title):
                return False
        if self.url is not None:
            if url is None or not self.url.search(url):
                return False
        return True


@dataclass(frozen=True)
class Classification:
    category: str
    subcategory: str | None
    rule_index: int | None  # None 이면 기본값으로 떨어진 것
    source: str  # 'rule' | 'default'


class Classifier:
    """rules.yaml 로부터 만들어지는 불변 분류기.

    `categories`/`default_category` 는 계약서가 명시한 public 속성이다.
    `browser_apps` 는 계약에는 없지만 rollup.py 가 "브라우저 앱일 때만 url 을
    붙인다" 를 판단하기 위해 필요해서 추가했다 — 두 파일이 같은 목록을
    rules.yaml 하나에서 공유하게 하려는 목적이다 (자세한 내용은 완료 보고 참조).
    """

    def __init__(
        self,
        categories: dict[str, Category],
        default_category: str,
        rules: list[Rule],
        browser_apps: frozenset[str],
    ) -> None:
        self.categories = categories
        self.default_category = default_category
        self.browser_apps = browser_apps
        self._rules = rules
        self._order = list(categories.keys())

    @classmethod
    def from_yaml(cls, path: str | Path) -> "Classifier":
        """rules.yaml 을 로드하고 정규식을 전부 컴파일한 Classifier 를 만든다."""
        raw_path = Path(path)
        with raw_path.open("r", encoding="utf-8") as f:
            doc = yaml.safe_load(f) or {}

        default_category = str(doc.get("default_category", "unknown"))

        categories: dict[str, Category] = {}
        for raw_cat in doc.get("categories", []):
            cat = Category(
                id=str(raw_cat["id"]),
                label=str(raw_cat["label"]),
                color=str(raw_cat["color"]),
            )
            categories[cat.id] = cat

        if default_category not in categories:
            raise ValueError(
                f"default_category({default_category!r}) 가 categories 목록에 없습니다: {raw_path}"
            )

        browser_apps = frozenset(
            str(a).strip().lower() for a in doc.get("browser_apps", []) if str(a).strip()
        )

        rules: list[Rule] = []
        for idx, raw_rule in enumerate(doc.get("rules", [])):
            category = str(raw_rule["category"])
            if category in _RESERVED_CATEGORIES:
                raise ValueError(
                    f"규칙 {idx} 이 예약 카테고리({category!r})를 직접 지정했습니다. "
                    "away/off/unknown 은 rollup/default_category 가 채웁니다."
                )
            if category not in categories:
                raise ValueError(f"규칙 {idx} 의 category({category!r})가 categories 목록에 없습니다.")

            match = raw_rule.get("match") or {}
            if not match:
                raise ValueError(f"규칙 {idx} 에 match 조건이 없습니다.")

            rule = Rule(
                index=idx,
                category=category,
                subcategory=(str(raw_rule["subcategory"]) if raw_rule.get("subcategory") else None),
                app=re.compile(str(match["app"])) if "app" in match else None,
                title=re.compile(str(match["title"])) if "title" in match else None,
                url=re.compile(str(match["url"])) if "url" in match else None,
            )
            rules.append(rule)

        return cls(categories=categories, default_category=default_category, rules=rules, browser_apps=browser_apps)

    def classify(
        self, app: str | None = None, title: str | None = None, url: str | None = None
    ) -> Classification:
        """규칙을 순서대로 평가해 첫 매칭을 반환한다. 없으면 default_category."""
        for rule in self._rules:
            if rule.matches(app, title, url):
                return Classification(
                    category=rule.category,
                    subcategory=rule.subcategory,
                    rule_index=rule.index,
                    source="rule",
                )
        return Classification(
            category=self.default_category, subcategory=None, rule_index=None, source="default"
        )

    def color(self, category_id: str) -> str:
        return self.categories[category_id].color

    def label(self, category_id: str) -> str:
        return self.categories[category_id].label

    def order(self) -> list[str]:
        """rules.yaml 의 categories 선언 순서 — 시각화 순서이자 동점 처리 우선순위."""
        return list(self._order)

    def is_browser(self, app: str | None) -> bool:
        """app 이 browser_apps 목록에 있는지 (대소문자 무시). rollup.py 의 url 부착 판정에 쓴다."""
        if not app:
            return False
        return app.strip().lower() in self.browser_apps


# 제목의 변동부를 지문 정규화할 때 쓰는 패턴들. 순서가 중요하다
# (GUID/경로처럼 구체적인 패턴을 먼저 치환해야 뒤의 숫자 치환이 GUID 안의
#  16진수까지 뭉개버리지 않는다).
_GUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_PATH_RE = re.compile(r"(?:[a-z]:)?(?:[\\/][^\s\\/]+){2,}")
_OF_COUNT_RE = re.compile(r"\d+\s+of\s+\d+")
_PAREN_COUNT_RE = re.compile(r"\(\d+\)")
_DIGITS_RE = re.compile(r"\d+")
_WS_RE = re.compile(r"\s+")


def normalize_fingerprint(app: str | None, title: str | None) -> str:
    """(app, title) 을 지문 문자열로 정규화한다.

    소문자화 + 공백 정규화 + 제목의 변동부(숫자, 경로, GUID, 알림 카운트,
    `— 3 of 12` 같은 페이지 표시)를 치환해, 실질적으로 같은 종류의 창이
    같은 지문으로 모이게 한다. app 과 title 은 U+001F(unit separator) 로 잇는다.
    """
    norm_app = _WS_RE.sub(" ", (app or "").strip().lower())

    norm_title = (title or "").strip().lower()
    if norm_title:
        norm_title = _GUID_RE.sub("guid", norm_title)
        norm_title = _PATH_RE.sub("path", norm_title)
        norm_title = _OF_COUNT_RE.sub("n of n", norm_title)
        norm_title = _PAREN_COUNT_RE.sub("(n)", norm_title)
        norm_title = _DIGITS_RE.sub("n", norm_title)
        norm_title = _WS_RE.sub(" ", norm_title).strip()

    return f"{norm_app}\x1f{norm_title}"
