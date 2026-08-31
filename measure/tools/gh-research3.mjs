// GitHub 저장소 조사 v3 — 미탐색 영역 보강
// 실행: node measure/tools/gh-research3.mjs <출력.json>
//
// 1·2차에서 다루지 않은 축: 음성, 워크플로우, 관측/평가, 임베딩, OCR,
// 홈오토메이션, 한국어 도구, 에이전트 벤치마크.

import { chromium } from 'playwright';
import fs from 'fs';

const OUT = process.argv[2] || '/home/user/project/project-jetson/measure/results/gh-research3.json';

const QUERIES = [
  // ═══ 음성 (젯슨 GPU 활용도 높음)
  ['voice', 'whisper realtime transcription local'],
  ['voice', 'topic:speech-to-text self-hosted'],
  ['voice', 'voice assistant local llm offline'],
  ['voice', 'topic:text-to-speech local'],
  // ═══ 워크플로우 자동화 (에이전트 실행 기반)
  ['workflow', 'topic:workflow-automation self-hosted'],
  ['workflow', 'low code ai workflow builder'],
  ['workflow', 'cron job scheduler llm pipeline'],
  // ═══ 에이전트 평가·관측
  ['eval', 'llm evaluation framework open source'],
  ['eval', 'agent benchmark tool use'],
  ['eval', 'topic:llm-observability'],
  ['eval', 'prompt testing regression llm'],
  // ═══ 임베딩·검색
  ['embed', 'topic:vector-database self-hosted'],
  ['embed', 'sentence embedding model multilingual'],
  ['embed', 'hybrid search bm25 vector'],
  // ═══ OCR·문서 이해
  ['ocr', 'topic:ocr deep learning'],
  ['ocr', 'document layout analysis table extraction'],
  ['ocr', 'pdf to markdown llm'],
  // ═══ 홈 오토메이션 / IoT (젯슨 상시가동 활용)
  ['home', 'topic:home-automation ai'],
  ['home', 'home assistant llm integration'],
  ['home', 'mqtt sensor data pipeline'],
  // ═══ 한국어 도구
  ['korean', 'korean language model tools'],
  ['korean', 'hangul nlp library'],
  ['korean', 'korean tokenizer morpheme'],
  // ═══ 모델 관리·라우팅
  ['ops', 'model registry local llm manager'],
  ['ops', 'gpu monitoring dashboard nvidia'],
  ['ops', 'topic:mlops self-hosted'],
  // ═══ 엣지 특화 최적화
  ['edge', 'speculative decoding inference'],
  ['edge', 'kv cache optimization llm'],
  ['edge', 'topic:quantization llm'],
];

const sleep = (ms) => new Promise(r => setTimeout(r, ms));

async function search(page, query, attempt = 1) {
  const url = `https://github.com/search?q=${encodeURIComponent(query)}&type=repositories&s=stars&o=desc`;
  try {
    await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 45000 });
    await page.waitForSelector('[data-testid="results-list"]', { timeout: 22000 });
  } catch {
    if (attempt < 3) {
      await sleep(15000 * attempt);
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
      out.push({
        repo: full,
        desc: t.slice(1).find(l => l.length > 25 && !/^[\d,.]+k?$/.test(l)) || '',
        stars: t.find(l => /^[\d,.]+k?$/.test(l)) || '',
        lang: t.find(l => /^(Python|C\+\+|TypeScript|JavaScript|Go|Rust|C|Java|Jupyter Notebook|Shell|C#|Kotlin|Swift|Cuda|HTML|Dockerfile|Vue|Svelte)$/.test(l)) || '',
        updated: t.find(l => /Updated|ago/i.test(l)) || '',
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
    }
    await sleep(8000);
  }
  await browser.close();
  const result = Object.values(all).map(r => ({
    repo: r.repo, stars: r.stars, lang: r.lang, updated: r.updated,
    desc: r.desc, cats: [...r.cats], hits: r.hits,
  }));
  fs.writeFileSync(OUT, JSON.stringify(result, null, 1));
  process.stderr.write(`\n성공 ${ok} / 실패 ${fail}\n총 ${result.length}개 → ${OUT}\n`);
})();
