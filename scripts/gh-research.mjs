// GitHub 저장소 조사 — Playwright
// 실행: node scripts/gh-research.mjs [출력파일]
//
// 여러 검색어를 순회하며 저장소 메타데이터를 수집한다.
// GitHub 검색 페이지는 JS 렌더링이라 정적 fetch로는 안 된다.

import { chromium } from 'playwright';
import fs from 'fs';

const OUT = process.argv[2] || '/home/user/project/project-jetson/results/gh-research.json';

// 카테고리별 검색어 — 자율주행 제외, 에이전트·엣지LLM·공개데이터 중심
const QUERIES = [
  // ── 에이전트 프레임워크
  ['agent', 'ai agent framework local llm'],
  ['agent', 'autonomous agent openai compatible'],
  ['agent', 'self-hosted ai agent tools'],
  ['agent', 'topic:ai-agent'],
  ['agent', 'topic:llm-agent'],
  ['agent', 'topic:agentic-ai'],
  ['agent', 'coding agent cli open source'],
  ['agent', 'computer use agent open source'],
  // ── 로컬 LLM 서빙·UI
  ['serving', 'topic:local-llm'],
  ['serving', 'llama.cpp server ui self-hosted'],
  ['serving', 'openai api compatible local inference'],
  // ── Jetson / 엣지
  ['jetson', 'topic:jetson'],
  ['jetson', 'jetson orin llm inference'],
  ['jetson', 'nvidia jetson containers ai'],
  ['jetson', 'edge ai deployment nvidia'],
  ['jetson', 'jetson vision pipeline deepstream'],
  // ── 공개 학습 데이터
  ['dataset', 'open llm training dataset'],
  ['dataset', 'topic:dataset language-model'],
  ['dataset', 'korean nlp dataset corpus'],
  ['dataset', 'synthetic data generation llm'],
  // ── RAG / 문서처리
  ['rag', 'topic:rag self-hosted'],
  ['rag', 'document parsing pdf hwp extraction'],
  ['rag', 'local embedding vector search offline'],
];

const sleep = (ms) => new Promise(r => setTimeout(r, ms));

async function search(page, query) {
  const url = `https://github.com/search?q=${encodeURIComponent(query)}&type=repositories&s=stars&o=desc`;
  try {
    await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 45000 });
  } catch (e) {
    console.error(`  navigate 실패: ${query} — ${e.message.slice(0, 60)}`);
    return [];
  }
  // 결과 목록 렌더링 대기
  try {
    await page.waitForSelector('[data-testid="results-list"]', { timeout: 25000 });
  } catch {
    console.error(`  결과 미표시: ${query}`);
    return [];
  }
  await sleep(1200);

  return await page.evaluate(() => {
    const out = [];
    const list = document.querySelector('[data-testid="results-list"]');
    if (!list) return out;
    for (const li of list.children) {
      const a = li.querySelector('a[href^="/"][href*="/"]');
      if (!a) continue;
      const full = a.getAttribute('href')?.replace(/^\//, '');
      if (!full || full.split('/').length !== 2) continue;
      const text = li.innerText || '';
      // 별 수: "12.3k" 형태
      const starM = text.match(/([\d,.]+k?)\s*\n?\s*$/mi);
      const starEl = li.querySelector('a[href$="/stargazers"], span[aria-label*="star"]');
      // 설명: 링크 다음 줄
      const lines = text.split('\n').map(s => s.trim()).filter(Boolean);
      out.push({
        repo: full,
        desc: lines.slice(1).find(l => l.length > 20 && !/^\d/.test(l)) || '',
        raw: lines.slice(0, 6).join(' | ').slice(0, 300),
        stars: (starEl?.innerText || starM?.[1] || '').trim(),
      });
    }
    return out;
  });
}

(async () => {
  const browser = await chromium.launch({
    headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage'],
  });
  const ctx = await browser.newContext({
    userAgent: 'Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
    viewport: { width: 1440, height: 2000 },
  });
  const page = await ctx.newPage();

  const all = {};
  for (const [cat, q] of QUERIES) {
    process.stderr.write(`[${cat}] ${q}\n`);
    const rows = await search(page, q);
    process.stderr.write(`  → ${rows.length}건\n`);
    for (const r of rows) {
      if (!all[r.repo]) all[r.repo] = { ...r, cats: new Set(), queries: [] };
      all[r.repo].cats.add(cat);
      all[r.repo].queries.push(q);
    }
    await sleep(2500);   // rate limit 배려
  }
  await browser.close();

  const result = Object.values(all).map(r => ({
    repo: r.repo, stars: r.stars, desc: r.desc, raw: r.raw,
    cats: [...r.cats], hits: r.queries.length,
  }));
  fs.writeFileSync(OUT, JSON.stringify(result, null, 1));
  process.stderr.write(`\n총 ${result.length}개 저장소 → ${OUT}\n`);
})();
