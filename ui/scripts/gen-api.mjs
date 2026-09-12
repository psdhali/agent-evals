#!/usr/bin/env node
// Generate src/api/schema.d.ts from the FastAPI OpenAPI spec (ADR-0009).
//
//   node scripts/gen-api.mjs          — from the committed src/api/openapi.json
//                                       (deterministic; CI needs no server).
//   npm run gen:api -- --live         — refresh openapi.json from the running
//                                       orchestrator API (API_SPEC_URL, default
//                                       http://127.0.0.1:8000/openapi.json),
//                                       then regenerate.  Commit the refreshed
//                                       spec alongside any backend model change.
//
// openapi-typescript v7 returns a TypeScript AST (ts.Node[]); astToString
// renders it back to source so the committed output is plain .d.ts text.

import { readFile, writeFile } from 'node:fs/promises';
import openapiTS, { astToString } from 'openapi-typescript';

const specUrl = new URL('../src/api/openapi.json', import.meta.url);
const outUrl = new URL('../src/api/schema.d.ts', import.meta.url);
const live = process.argv.includes('--live');
const apiUrl = process.env.API_SPEC_URL ?? 'http://127.0.0.1:8000/openapi.json';

let raw;
if (live) {
  const res = await fetch(apiUrl);
  if (!res.ok) {
    console.error(`gen-api: openapi fetch failed (${res.status}): ${apiUrl}`);
    process.exit(1);
  }
  raw = await res.text();
} else {
  raw = await readFile(specUrl, 'utf8');
}

try {
  const ast = await openapiTS(JSON.parse(raw));
  const banner =
    '// GENERATED FILE - do not edit by hand.\n' +
    '// npm run gen:api regenerates this from src/api/openapi.json (ADR-0009);\n' +
    '// npm run gen:api -- --live refreshes the spec from the live API first.\n\n';
  await writeFile(outUrl, banner + astToString(ast));
  if (live) {
    await writeFile(specUrl, raw);
  }
  console.log(
    `gen:api wrote src/api/schema.d.ts (${live ? 'live' : 'frozen'} spec)`,
  );
} catch (err) {
  console.error('gen:api failed:', err?.message ?? err);
  process.exit(1);
}
