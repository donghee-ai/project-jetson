"""계약서에 적힌 시그니처를 실제 코드와 대조한다."""
import ast, re, sys, json
from pathlib import Path

ROOT = Path("/home/user/project/project-jetson/life-trainer")
DOCS = ["docs/contracts.md"]   # 통합본. 원본을 재검증하려면 archive/ 의 세 파일로 바꾼다

HDR = re.compile(r"^### `([^`]+)`")
FENCE = re.compile(r"^```(\w*)\s*$")
DEF  = re.compile(r"^\s*(?:async\s+)?def\s+(\w+)\s*\(")
CLS  = re.compile(r"^\s*class\s+(\w+)")
CONST= re.compile(r"^([A-Z_][A-Z0-9_]*)\s*[:=]")

def claims():
    out = []   # (doc, target, kind, name)
    for d in DOCS:
        target, inblk = None, False
        for line in (ROOT/d).read_text(encoding="utf-8").splitlines():
            h = HDR.match(line)
            if h:
                target = h.group(1).split(" ")[0].strip("`"); inblk = False; continue
            f = FENCE.match(line)
            if f:
                inblk = (not inblk) and f.group(1) in ("python", "py")
                continue
            if not (inblk and target and target.endswith(".py")):
                continue
            for rx, kind in ((DEF,"def"), (CLS,"class"), (CONST,"const")):
                m = rx.match(line)
                if m:
                    out.append((d, target, kind, m.group(1))); break
    return out

def actual(path):
    p = ROOT/path
    if not p.exists(): return None
    try: tree = ast.parse(p.read_text(encoding="utf-8"))
    except SyntaxError: return None
    names = set()
    for n in ast.walk(tree):
        if isinstance(n,(ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)): names.add(n.name)
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name): names.add(n.target.id)
        elif isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name): names.add(t.id)
    return names

cache, rows = {}, []
for doc, target, kind, name in claims():
    if target not in cache: cache[target] = actual(target)
    have = cache[target]
    if have is None:  status = "FILE_MISSING"
    elif name in have: status = "OK"
    else:              status = "GONE"
    rows.append(dict(doc=doc, target=target, kind=kind, name=name, status=status))

print(json.dumps(rows, ensure_ascii=False))
