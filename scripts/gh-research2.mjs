// GitHub 저장소 조사 v2 — 레이트 리밋 대응 + 검색어 대폭 확장
// 실행: node scripts/gh-research2.mjs <출력.json>
//
// v1에서 24개 중 12개가 레이트 리밋으로 실패 → 간격 8초, 실패 시 2회 재시도.

import { chromium } from 'playwright';
import fs from 'fs';

const OUT = process.argv[2] || '/home/user/project/project-jetson/results/gh-research2.json';

const QUERIES = [
  // ═══ 에이전트 프레임워크 (핵심 관심사)
  ['agent-fw', 'self-hosted ai agent tools'],
  ['agent-fw', 'topic:ai-agent'],
  ['agent-fw', 'coding agent cli open source'],
  ['agent-fw', 'computer use agent open source'],
  ['agent-fw', 'topic:autonomous-agents'],
  ['agent-fw', 'topic:agent-framework'],
  ['agent-fw', 'multi agent orchestration llm'],
  ['agent-fw', 'agent memory long term llm'],
  ['agent-fw', 'browser automation agent llm'],
  ['agent-fw', 'terminal ai assistant shell'],
  // ═══ MCP (에이전트 툴 생태계)
  ['mcp', 'topic:mcp-server'],
  ['mcp', 'model context protocol server'],
  ['mcp', 'topic:model-context-protocol'],
  // ═══ 로컬 LLM 서빙·UI
  ['serving', 'openai api compatible local inference'],
  ['serving', 'topic:llamacpp'],
  ['serving', 'chat ui self hosted llm'],
  ['serving', 'llm gateway proxy router'],
  ['serving', 'topic:ollama'],
  // ═══ Jetson / 엣지 AI
  ['jetson', 'topic:jetson'],
  ['jetson', 'jetson vision pipeline deepstream'],
  ['jetson', 'edge ai deployment nvidia'],
  ['jetson', 'topic:jetson-nano'],
  ['jetson', 'tensorrt optimization inference'],
  ['jetson', 'topic:edge-computing ai'],
  ['jetson', 'nvidia dla accelerator'],
  // ═══ 영상·비전 파이프라인 (자율주행 제외)
  ['vision', 'video analytics llm search'],
  ['vision', 'topic:object-detection realtime edge'],
  ['vision', 'cctv ai monitoring open source'],
  ['vision', 'vlm video understanding local'],
  // ═══ 공개 학습 데이터
  ['dataset', 'korean nlp dataset corpus'],
  ['dataset', 'synthetic data generation llm'],
  ['dataset', 'topic:open-data ai'],
  ['dataset', 'instruction tuning dataset open'],
  ['dataset', 'data curation pipeline llm training'],
  ['dataset', 'web crawl dataset common crawl'],
  // ═══ RAG / 문서처리
  ['rag', 'document parsing pdf extraction layout'],
  ['rag', 'local embedding vector search offline'],
  ['rag', 'topic:retrieval-augmented-generation'],
  ['rag', 'knowledge base self hosted ai'],
  ['rag', 'hwp korean document parser'],
  // ═══ 파인튜닝 / 경량화
  ['finetune', 'lora finetuning consumer gpu'],
  ['finetune', 'quantization gguf tools'],
  ['finetune', 'topic:model-compression'],
];

const sleep = (ms) => new Promise(r => setTimeout(r, ms));

async function search(page, query, attempt = 1) {
  const url = `https://github.com/search?q=${encodeURIComponent(query)}&type=repositories&s=stars&o=desc`;
  try {
    await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 45000 });
    await page.waitForSelector('[data-testid="results-list"]', { timeout: 22000 });
  } catch {
    if (attempt < 3) {
      const wait = 15000 * attempt;
      process.stderr.write(`    재시도 ${attempt} (${wait / 1000}초 대기)\n`);
      await sleep(wait);
      return search(page, query, attempt + 1);
    }
    return [];
  }
  await sleep(1500);

  return await page.evaluate(() => {
    const out = [];
    const list = document.querySelector('[data-testid="results-list"]');
    if (!list) return out;
    for (const li of list.children) {
      const a = li.querySelector('a[href^="/"]');
      const full = a?.getAttribute('href')?.replace(/^\//, '');
      if (!full || full.split('/').length !== 2) continue;
      const t = (li.innerText || '').split('\n').map(s => s.trim()).filter(Boolean);
      // 마지막 줄들에 언어/별/갱신일이 섞여 있다
      const starLine = t.find(l => /^[\d,.]+k?$/.test(l));
      const updated = t.find(l => /Updated|ago|주 전|일 전/i.test(l));
      const lang = t.find(l => /^(Python|C\+\+|TypeScript|JavaScript|Go|Rust|C|Java|Jupyter Notebook|Shell|C#|Kotlin|Swift|Cuda|HTML|Dockerfile)$/.test(l));
      out.push({
        repo: full,
        desc: t.slice(1).find(l => l.length > 25 && !/^[\d,.]+k?$/.test(l)) || '',
        stars: starLine || '',
        lang: lang || '',
        updated: updated || '',
      });
    }
    return out;
  });
}

(async () => {
  const browser = await chromium.launch({ headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  const ctx = await browser.newContext({
    userAgent: 'Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
    viewport: { width: 1440, height: 2400 },
  });
  const page = await ctx.newPage();

  const all = {};
  let ok = 0, fail = 0;
  for (const [cat, q] of QUERIES) {
    process.stderr.write(`[${cat}] ${q}\n`);
    const rows = await search(page, q);
    rows.length ? ok++ : fail++;
    process.stderr.write(`  → ${rows.length}건\n`);
    for (const r of rows) {
      if (!all[r.repo]) all[r.repo] = { ...r, cats: new Set(), hits: 0 };
      all[r.repo].cats.add(cat);
      all[r.repo].hits++;
      if (!all[r.repo].stars && r.stars) all[r.repo].stars = r.stars;
      if (!all[r.repo].desc && r.desc) all[r.repo].desc = r.desc;
    }
    await sleep(8000);   // 레이트 리밋 대응
  }
  await browser.close();

  const result = Object.values(all).map(r => ({
    repo: r.repo, stars: r.stars, lang: r.lang, updated: r.updated,
    desc: r.desc, cats: [...r.cats], hits: r.hits,
  }));
  fs.writeFileSync(OUT, JSON.stringify(result, null, 1));
  process.stderr.write(`\n검색 성공 ${ok} / 실패 ${fail}\n총 ${result.length}개 저장소 → ${OUT}\n`);
})();
