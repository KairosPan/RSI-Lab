import test from "node:test";
import assert from "node:assert/strict";
import { access, copyFile, mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { createHash } from "node:crypto";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { experiment } from "../src/experiment-data.js";
import { buildExperiment, renderExperiment } from "../scripts/import-experiment.mjs";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const read = async (path) => JSON.parse(await readFile(resolve(root, path), "utf8"));
const hasLocalSources = await access(resolve(root, "runs/lab_comparison_v2_readout/summary.json"))
  .then(() => true).catch((error) => { if (error.code === "ENOENT") return false; throw error; });
const sourceOnly = { skip: hasLocalSources ? false : "Local experiment artifacts are not present; portable snapshot checks still run." };

test("portable snapshot has internally consistent arm quality and token accounting", () => {
  assert.equal(experiment.id, "lab_comparison_v2");
  assert.ok(Number.isFinite(Date.parse(experiment.generatedAt)));
  assert.deepEqual(experiment.arms.map((arm) => arm.id), ["baseline", "H0", "H1"]);
  for (const arm of experiment.arms) {
    const final = arm.shortlist.map((id) => experiment.candidates.find((candidate) => candidate.id === id).results[arm.id]);
    const valid = final.filter((result) => result.pairedValid);
    assert.equal(arm.quality.validCount, valid.length);
    assert.ok(Math.abs(arm.quality.meanCnn - valid.reduce((sum, result) => sum + result.cnnMedian, 0) / valid.length) < 1e-12);
    assert.ok(Math.abs(arm.quality.meanEnergy - valid.reduce((sum, result) => sum + result.energyMedian, 0) / valid.length) < 1e-12);
    assert.equal(arm.usage.totalTokens, arm.usage.inputTokens + arm.usage.outputTokens);
    assert.equal(arm.usage.costUsd, null);
    assert.equal(arm.pendingMemoryCount, arm.memoryCount);
  }
  assert.equal(experiment.meta.totalTokens, 105031);
  assert.equal(experiment.meta.totalTokens + experiment.arms.find((arm) => arm.id === "H1").usage.totalTokens, 242552);
  assert.equal(experiment.meta.releaseAuthorized, false);
});

test("all 20 candidates retain identities and uncomputed results stay absent", () => {
  assert.equal(experiment.candidates.length, 20);
  for (const candidate of experiment.candidates) {
    assert.match(candidate.id, /^CHEMBL\d+$/);
    assert.ok(candidate.smiles.length > 0 && candidate.activityIds.length > 0 && candidate.documentIds.length > 0);
    assert.ok(candidate.ic50Nm > 0);
    for (const [arm, result] of Object.entries(candidate.results)) {
      assert.equal(result.shortlisted, experiment.arms.find((item) => item.id === arm).shortlist.includes(candidate.id));
      assert.equal(result.jobs.length, 2);
      assert.equal(result.validRepeats, result.jobs.filter((job) => job.poseValid).length);
      if (!result.pairedValid) assert.equal(result.cnnMedian, null);
    }
  }
  for (const arm of experiment.arms) {
    assert.equal(experiment.candidates.filter((candidate) => Object.hasOwn(candidate.results, arm.id)).length, 8);
    assert.equal(experiment.candidates.filter((candidate) => !Object.hasOwn(candidate.results, arm.id)).length, 12);
  }
});

test("pose validity and final shortlist validity have distinct denominators", () => {
  for (const arm of experiment.arms) {
    const jobs = experiment.candidates.flatMap((candidate) => candidate.results[arm.id]?.jobs ?? []);
    assert.equal(jobs.length, arm.numerical.jobs);
    assert.equal(jobs.length, 16);
    assert.equal(jobs.filter((job) => job.poseValid).length, arm.numerical.poseValidJobs);
    assert.equal(arm.numerical.poseValidJobs, 13);
    assert.equal(arm.numerical.terminalJobs, 0);
    assert.equal(arm.quality.validCount, 5);
    assert.equal(arm.quality.shortlistSize, 5);
  }
});

test("evidence records are complete, unique, linked and separated by kind", () => {
  const evidenceById = new Map(experiment.evidence.map((entry) => [entry.id, entry]));
  assert.equal(evidenceById.size, experiment.evidence.length);
  assert.equal(experiment.evidence.filter((entry) => entry.kind === "public_measurement").length, 20);
  assert.equal(experiment.evidence.filter((entry) => entry.kind === "actual_computational_results").length, 24);
  for (const candidate of experiment.candidates) {
    assert.ok(evidenceById.has(`public-${candidate.id}`));
    for (const [arm, result] of Object.entries(candidate.results)) {
      const entry = evidenceById.get(result.evidenceId);
      assert.equal(entry.arm, arm);
      assert.equal(entry.candidateId, candidate.id);
      assert.equal(entry.record.officialAffinityPass, null);
    }
  }
});

test("source manifest identifies local roots and covers every displayed source", () => {
  const paths = new Set();
  for (const source of experiment.sources) {
    assert.ok(!source.path.startsWith("/") && !source.path.includes(".."));
    assert.match(source.sha256, /^[a-f0-9]{64}$/);
    assert.ok(["recorded_binding", "import_hash_only"].includes(source.verification));
    paths.add(source.path);
  }
  for (const entry of [...experiment.arms, ...experiment.evidence, experiment.meta, experiment.audit]) assert.ok(paths.has(entry.source));
  for (const candidate of experiment.candidates) for (const result of Object.values(candidate.results)) {
    assert.ok(paths.has(result.source));
    for (const job of result.jobs) assert.ok(paths.has(job.source));
  }
});

test("portable snapshot does not expose local paths or Chinese UI strings", () => {
  assert.doesNotMatch(renderExperiment(experiment), /\/Users\/|\/home\/|\/content\/|[\u3400-\u9fff]/u);
});

test("local artifacts match snapshot aggregates and candidate projections", sourceOnly, async () => {
  const summary = await read("runs/lab_comparison_v2_readout/summary.json");
  const pool = await read("data/lab_cdk2_v2/candidates.json");
  assert.equal(experiment.generatedAt, summary.generated_at);
  for (const arm of experiment.arms) {
    const source = summary.arms[arm.id];
    assert.deepEqual(arm.shortlist, source.shortlist);
    assert.equal(arm.quality.validCount, source.quality.paired_valid_count);
    assert.ok(Math.abs(arm.quality.meanCnn - source.quality.mean_qualified_median_CNNscore) < 1e-12);
    assert.ok(Math.abs(arm.quality.meanEnergy - source.quality.mean_qualified_median_minimizedAffinity) < 1e-12);
    assert.equal(arm.usage.totalTokens, source.usage.observed_input_plus_output_tokens);
  }
  for (const candidate of experiment.candidates) {
    const original = pool.candidates.find((item) => item.candidate_id === candidate.id);
    assert.equal(candidate.smiles, original.smiles);
    assert.equal(candidate.ic50Nm, original.ic50_nM);
    assert.deepEqual(candidate.activityIds, original.activity_ids);
    assert.deepEqual(candidate.documentIds, original.document_ids);
  }
});

test("local source manifest hashes match original file bytes", sourceOnly, async () => {
  for (const source of experiment.sources) {
    const bytes = await readFile(resolve(root, source.path));
    assert.equal(createHash("sha256").update(bytes).digest("hex"), source.sha256, source.path);
  }
});

test("local regeneration is deterministic", sourceOnly, async () => {
  const rebuilt = await buildExperiment(root);
  assert.deepEqual(rebuilt, experiment);
  const emitted = await readFile(resolve(root, "frontend/src/experiment-data.js"), "utf8");
  assert.equal(renderExperiment(rebuilt), emitted);
});

test("import rejects post-audit baseline job, Meta usage and binding changes", sourceOnly, async (t) => {
  const fixtureRoot = await mkdtemp(resolve(tmpdir(), "rsi-import-test-"));
  t.after(() => rm(fixtureRoot, { recursive: true, force: true }));
  await Promise.all(experiment.sources.map(async ({ path }) => {
    const target = resolve(fixtureRoot, path);
    await mkdir(dirname(target), { recursive: true });
    await copyFile(resolve(root, path), target);
  }));
  const jobPath = experiment.sources.find(({ path }) => /fixed_baseline\/batches\/.*\/jobs\/.*\/result\.json$/.test(path)).path;
  for (const [path, mutate] of [
    [jobPath, (record) => { record.scores.CNNscore += 0.001; }],
    ["runs/meta_h0_to_h1_v1/usage.json", (record) => { record.wall_seconds += 1; }],
    ["runs/meta_h0_to_h1_v1/proposal_binding.json", (record) => { record.release_authorized = true; }],
  ]) {
    const target = resolve(fixtureRoot, path);
    const bytes = await readFile(target);
    const modified = JSON.parse(bytes.toString("utf8"));
    mutate(modified);
    await writeFile(target, JSON.stringify(modified));
    await assert.rejects(buildExperiment(fixtureRoot), /Hash binding changed/);
    await writeFile(target, bytes);
  }
});
