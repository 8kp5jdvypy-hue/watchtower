#!/usr/bin/env node
// Validates JSON payloads against the iOS app's OWN zod schemas
// (perch-mobile-mvp/src/api/schemas.ts) -- the contract /v1 must meet.
//
// Usage: node scripts/v1_schema_check.mjs <path/to/schemas.ts> < payloads.json
//   payloads.json: {"<SchemaName>": [payload, payload, ...], ...}
// Exit 0 when everything parses; otherwise prints each failure and exits 1.
//
// Node >= 22.6 strips TypeScript types natively, and schemas.ts uses only
// erasable syntax, so no build step is needed. zod is resolved from the
// app's own node_modules by importing the schema file from its location.
import { pathToFileURL } from 'node:url';
import { readFileSync } from 'node:fs';

const schemasPath = process.argv[2];
if (!schemasPath) {
  console.error('usage: v1_schema_check.mjs <schemas.ts> < payloads.json');
  process.exit(2);
}
const schemas = await import(pathToFileURL(schemasPath).href);
const payloads = JSON.parse(readFileSync(0, 'utf8'));

let failures = 0;
let checked = 0;
for (const [name, items] of Object.entries(payloads)) {
  const schema = schemas[name];
  if (!schema) {
    console.error(`no schema named ${name} in ${schemasPath}`);
    failures += 1;
    continue;
  }
  for (const [index, item] of items.entries()) {
    checked += 1;
    const result = schema.safeParse(item);
    if (!result.success) {
      failures += 1;
      console.error(`FAIL ${name}[${index}]:`);
      for (const issue of result.error.issues) {
        console.error(`  ${issue.path.join('.') || '(root)'}: ${issue.message}`);
      }
    }
  }
}
console.log(`${checked} payload(s) checked, ${failures} failure(s)`);
process.exit(failures ? 1 : 0);
