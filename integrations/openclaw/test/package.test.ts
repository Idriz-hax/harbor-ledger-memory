import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { test } from "node:test";

test("package metadata points to compiled plugin entrypoint", () => {
  const root = resolve(import.meta.dirname, "..");
  const pkg = JSON.parse(readFileSync(resolve(root, "package.json"), "utf8"));
  assert.equal(pkg.exports, "./dist/index.js");
  assert.deepEqual(pkg.openclaw.extensions, ["./dist/index.js"]);
  assert.ok(existsSync(resolve(root, "dist/index.js")), "build dist/index.js before running this check");
});

test("manifest configSchema accepts source SecretRefs and resolved string tokens", () => {
  const root = resolve(import.meta.dirname, "..");
  const manifest = JSON.parse(readFileSync(resolve(root, "openclaw.plugin.json"), "utf8"));
  const tokenSchema = manifest.configSchema.properties.token;
  assert.ok(Array.isArray(tokenSchema.anyOf), "token schema must define source and resolved forms");
  assert.ok(tokenSchema.anyOf.some((branch: { type?: string }) => branch.type === "string"), "resolved runtime token remains a string");
  const secretRef = { source: "env", provider: "default", id: "HLM_TOKEN" };
  const refSchema = tokenSchema.anyOf.find((branch: { type?: string }) => branch.type === "object");
  assert.ok(refSchema, "persisted OpenClaw SecretRef object must be accepted");
  assert.deepEqual(refSchema.required, ["source", "provider", "id"]);
  assert.equal(refSchema.additionalProperties, false);
  for (const key of refSchema.required) assert.equal(refSchema.properties[key].type, "string");
  assert.deepEqual(secretRef, { source: "env", provider: "default", id: "HLM_TOKEN" });
});
