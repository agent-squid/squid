import { readFile } from "node:fs/promises";
import { pathToFileURL } from "node:url";

const modulePath = process.env.MERMAID_MODULE;
if (!modulePath) throw new Error("MERMAID_MODULE must point to mermaid.esm.mjs");
const { default: mermaid } = await import(pathToFileURL(modulePath).href);

let failed = false;
for (const file of process.argv.slice(2)) {
  const markdown = await readFile(file, "utf8");
  const diagrams = [...markdown.matchAll(/^```mermaid\s*\n([\s\S]*?)^```\s*$/gm)];
  for (const [index, match] of diagrams.entries()) {
    try {
      await mermaid.parse(match[1]);
    } catch (error) {
      failed = true;
      console.error(`${file}: Mermaid block ${index + 1} is invalid`);
      console.error(error instanceof Error ? error.message : error);
    }
  }
}

if (failed) process.exitCode = 1;
