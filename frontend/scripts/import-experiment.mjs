import { readFile, writeFile } from "node:fs/promises";
import { createHash } from "node:crypto";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import assert from "node:assert/strict";

const DEFAULT_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const ARM_FOLDERS = { baseline: "fixed_baseline", H0: "H0", H1: "H1" };
const ARM_LABELS = { baseline: "Fixed baseline", H0: "H0 · Expanded JSON", H1: "H1 · Compact JSON candidate" };
const sha256 = (bytes) => createHash("sha256").update(bytes).digest("hex");
const sum = (values) => values.reduce((total, value) => total + value, 0);
const mean = (values) => values.length ? sum(values) / values.length : null;
const close = (actual, expected, field) => {
  if (actual === null || expected === null) assert.equal(actual, expected, field);
  else assert.ok(Number.isFinite(actual) && Math.abs(actual - expected) < 1e-8, `${field}: source mismatch`);
};

/** Read immutable artifacts only; build a portable, English presentation projection. */
export async function buildExperiment(repoRoot = DEFAULT_ROOT) {
  const sources = new Map();
  const sourceChecks = new Map();
  const read = async (path, expectedHash) => {
    assert.ok(!path.startsWith("/") && !path.split("/").includes(".."), "Source must be repo-relative");
    const bytes = await readFile(resolve(repoRoot, path));
    const hash = sha256(bytes);
    if (expectedHash) assert.equal(hash, expectedHash, `Hash binding changed: ${path}`);
    sources.set(path, hash);
    sourceChecks.set(path, expectedHash ? "recorded_binding" : "import_hash_only");
    return JSON.parse(bytes.toString("utf8"));
  };

  const summaryPath = "runs/lab_comparison_v2_readout/summary.json";
  const summary = await read(summaryPath);
  const preregPath = "runs/lab_comparison_v2/preregistration.json";
  const prereg = await read(preregPath, summary.preregistration_sha256);
  const protocolPath = "protocols/lab_comparison_v2.json";
  const protocol = await read(protocolPath, summary.protocol_sha256);
  const auditPath = "runs/lab_comparison_v2_audit/audit.json";
  const audit = await read(auditPath, summary.overall_audit_sha256);
  const candidatePath = "data/lab_cdk2_v2/candidates.json";
  const candidateData = await read(candidatePath, prereg.candidates_sha256);
  const manifest = await read("data/lab_cdk2_v2/manifest.json", prereg.manifest_sha256);
  const requiredHash = (value, name) => {
    assert.match(value ?? "", /^[a-f0-9]{64}$/, `Missing recorded hash: ${name}`);
    return value;
  };
  const metaManifest = await read("runs/meta_h0_to_h1_v1/artifact_hashes.json");
  const metaStatePath = "runs/meta_h0_to_h1_v1/state.json";
  const metaState = await read(metaStatePath, requiredHash(prereg.frozen_files[metaStatePath], metaStatePath));
  const metaReviewPath = "runs/meta_h0_to_h1_v1_review/controller_review.json";
  const metaReview = await read(metaReviewPath, requiredHash(prereg.frozen_files[metaReviewPath], metaReviewPath));
  const bindingPath = "runs/meta_h0_to_h1_v1/proposal_binding.json";
  const binding = await read(bindingPath, requiredHash(metaManifest["proposal_binding.json"], bindingPath));
  const metaResponsePath = "runs/meta_h0_to_h1_v1/response.json";
  const metaResponse = await read(metaResponsePath, requiredHash(prereg.frozen_files[metaResponsePath], metaResponsePath));
  assert.equal(binding.response_sha256, prereg.frozen_files[metaResponsePath]);
  assert.equal(binding.config_sha256, prereg.frozen_files["runs/lab_comparison_v2/h1_config.json"]);
  assert.equal(binding.release_authorized, metaState.release_authorized);
  assert.equal(binding.release_authorized, metaReview.release_authorized);
  await read("runs/meta_h0_to_h1_v1/h1_config.json", binding.config_sha256);
  const metaUsage = await read("runs/meta_h0_to_h1_v1/usage.json", requiredHash(metaManifest["usage.json"], "Meta usage"));
  assert.deepEqual(metaUsage, metaState.calls[0], "Meta usage must match the preregistration-bound state call");
  assert.equal(summary.comparison_id, prereg.comparison_id);
  assert.equal(audit.comparison_id, prereg.comparison_id);
  assert.equal(audit.protocol_sha256, summary.protocol_sha256);
  assert.equal(candidateData.is_mock, false, "Expected actual candidate data");
  assert.equal(candidateData.candidates.length, prereg.pool_size);
  assert.equal(manifest.assay_chembl_id, prereg.assay_id);

  const candidates = candidateData.candidates.map((candidate) => ({
    id: candidate.candidate_id,
    smiles: candidate.smiles,
    ic50Nm: candidate.ic50_nM,
    assayId: candidate.assay_id,
    scaffold: candidate.structure.scaffold_id,
    activityIds: candidate.activity_ids,
    documentIds: candidate.document_ids,
    results: {},
  }));
  const byId = new Map(candidates.map((candidate) => [candidate.id, candidate]));
  assert.equal(byId.size, prereg.pool_size, "Candidate IDs must be unique");
  const evidence = candidates.map((candidate) => ({
    id: `public-${candidate.id}`,
    arm: "shared",
    candidateId: candidate.id,
    kind: "public_measurement",
    title: `Public IC50 · ${candidate.id}`,
    source: candidatePath,
    detail: "Historical ChEMBL assay measurement available to every arm; not a new experimental result or hidden evaluation label.",
    record: {
      assayId: candidate.assayId, ic50Nm: candidate.ic50Nm, unit: "nM",
      activityIds: candidate.activityIds, documentIds: candidate.documentIds,
      database: manifest.database, databaseVersion: manifest.database_version,
    },
  }));
  const arms = [];

  for (const [id, folder] of Object.entries(ARM_FOLDERS)) {
    const base = `runs/lab_comparison_v2/${folder}`;
    const statePath = `${base}/state.json`;
    const evaluationPath = `${base}/evaluation.json`;
    const state = await read(statePath, audit.arm_audits[id].state_sha256);
    const evaluation = await read(evaluationPath, summary.arms[id].evaluation_sha256);
    const armAuditPath = id === "baseline" ? `${base}/audit.json` : `runs/lab_comparison_v2_audit/${id}_audit.json`;
    await read(armAuditPath, audit.arm_audits[id].sha256);
    const readout = summary.arms[id];
    assert.equal(evaluation.status, readout.arm ? "completed" : summary.status);
    assert.deepEqual(evaluation.shortlist, readout.shortlist, `${id} locked shortlist`);
    assert.deepEqual(state.candidates.map((item) => item.candidate_id), candidates.map((item) => item.id), `${id} candidate pool`);
    const shortlisted = new Map(evaluation.shortlist.map((candidateId, index) => [candidateId, index + 1]));
    const allJobs = [];
    const batchManifests = new Map();

    for (const [candidateId, result] of Object.entries(state.results)) {
      const candidate = byId.get(candidateId);
      assert.ok(candidate, "Calculated candidate must be in the frozen pool");
      const rawJobs = [];
      if (id === "baseline") {
        assert.match(result.artifact_ref, /^batches\/[a-zA-Z0-9_]+$/);
        const requestId = result.artifact_ref.split("/")[1];
        if (!batchManifests.has(requestId)) {
          const manifestPath = `${base}/${result.artifact_ref}/artifact_hashes.json`;
          const batchManifest = await read(manifestPath, requiredHash(state.requests[requestId]?.artifacts_sha256, manifestPath));
          batchManifests.set(requestId, batchManifest);
        }
        for (const seed of protocol.seeds) {
          const jobRef = `jobs/${candidateId}_s${seed}/result.json`;
          const path = `${base}/${result.artifact_ref}/${jobRef}`;
          rawJobs.push({ raw: await read(path, requiredHash(batchManifests.get(requestId)[jobRef], path)), source: path });
        }
      } else {
        assert.equal(result.protocol_sha256, summary.protocol_sha256);
        for (const raw of result.jobs) rawJobs.push({ raw, source: statePath });
      }
      const jobs = rawJobs.map(({ raw, source }) => ({
        seed: raw.seed, status: raw.status,
        cnnScore: raw.scores?.CNNscore ?? null,
        energy: raw.scores?.minimizedAffinity ?? null,
        cnnAffinity: raw.scores?.CNNaffinity ?? null,
        poseValid: raw.pose_valid,
        identityPreserved: raw.identity_preserved,
        failedChecks: Object.entries(raw.pose_checks ?? {}).filter(([, passed]) => passed === false).map(([name]) => name),
        cpuSeconds: raw.cpu_seconds ?? null, wallSeconds: raw.wall_seconds ?? null,
        gpuObserved: raw.gpu_process_observed, source,
      }));
      assert.equal(jobs.filter((job) => job.status === "completed").length, result.completed_repeats);
      assert.equal(jobs.filter((job) => job.poseValid).length, result.valid_repeats);
      allJobs.push(...jobs);
      const evidenceId = `${id}-${result.evidence_id ?? `calculation-${candidateId}`}`;
      candidate.results[id] = {
        completedRepeats: result.completed_repeats,
        validRepeats: result.valid_repeats,
        pairedValid: result.two_valid_repeats,
        cnnMedian: result.qualified_median_CNNscore,
        energyMedian: result.qualified_median_minimizedAffinity,
        cnnRange: result.seed_range_CNNscore,
        shortlisted: shortlisted.has(candidateId), rank: shortlisted.get(candidateId) ?? null,
        evidenceId, source: statePath, jobs,
      };
      evidence.push({
        id: evidenceId, arm: id, candidateId, kind: "actual_computational_results",
        title: `GNINA paired calculation · ${candidateId}`,
        source: statePath,
        detail: "Recorded GNINA calculations with pose-validity and identity checks. CNNscore is a computational ranking signal, not biological activity. Qualified medians are absent when the paired-valid requirement is not met.",
        record: {
          protocolId: protocol.protocol_id, protocolHash: summary.protocol_sha256,
          completedRepeats: result.completed_repeats, validRepeats: result.valid_repeats,
          pairedValid: result.two_valid_repeats,
          rawCnnMedian: result.raw_median_CNNscore,
          qualifiedCnnMedian: result.qualified_median_CNNscore,
          rawEnergyMedian: result.raw_median_minimizedAffinity,
          qualifiedEnergyMedian: result.qualified_median_minimizedAffinity,
          energyUnit: "kcal/mol", cnnRange: result.seed_range_CNNscore,
          officialAffinityPass: null, jobs,
        },
      });
    }

    const finalResults = evaluation.shortlist.map((candidateId) => byId.get(candidateId).results[id]);
    const qualified = finalResults.filter((result) => result.pairedValid);
    const quality = {
      validCount: qualified.length,
      shortlistSize: evaluation.shortlist_size,
      meanCnn: mean(qualified.map((result) => result.cnnMedian)),
      meanEnergy: mean(qualified.map((result) => result.energyMedian)),
      meanCnnRange: mean(finalResults.map((result) => result.cnnRange)),
      maxCnnRange: Math.max(...finalResults.map((result) => result.cnnRange)),
    };
    close(quality.validCount, readout.quality.paired_valid_count, `${id} validCount`);
    close(quality.meanCnn, readout.quality.mean_qualified_median_CNNscore, `${id} meanCnn`);
    close(quality.meanEnergy, readout.quality.mean_qualified_median_minimizedAffinity, `${id} meanEnergy`);
    close(quality.meanCnnRange, readout.quality.mean_seed_range_CNNscore, `${id} meanCnnRange`);
    close(quality.maxCnnRange, readout.quality.max_seed_range_CNNscore, `${id} maxCnnRange`);
    const usage = {
      calls: state.llm_calls.length,
      inputTokens: sum(state.llm_calls.map((call) => call.usage.input_tokens)),
      outputTokens: sum(state.llm_calls.map((call) => call.usage.output_tokens)),
      totalTokens: sum(state.llm_calls.map((call) => call.usage.input_tokens + call.usage.output_tokens)),
      llmWallSeconds: sum(state.llm_calls.map((call) => call.wall_seconds)),
      costUsd: readout.usage.actual_cost_usd,
    };
    close(usage.totalTokens, readout.usage.observed_input_plus_output_tokens, `${id} totalTokens`);
    close(usage.inputTokens, readout.usage.input_tokens, `${id} inputTokens`);
    close(usage.outputTokens, readout.usage.output_tokens, `${id} outputTokens`);
    close(usage.llmWallSeconds, readout.usage.llm_wall_seconds, `${id} llmWallSeconds`);
    const numerical = {
      onlineCandidates: readout.numerical.online.candidates,
      jobs: allJobs.length,
      completedJobs: allJobs.filter((job) => job.status === "completed").length,
      poseValidJobs: allJobs.filter((job) => job.poseValid).length,
      batchWallSeconds: sum(Object.values(state.requests).map((request) => request.actual_wall_seconds)),
      cpuSeconds: allJobs.every((job) => job.cpuSeconds !== null) ? sum(allJobs.map((job) => job.cpuSeconds)) : null,
      gpuObservedJobs: allJobs.filter((job) => job.gpuObserved).length,
      terminalJobs: readout.numerical.terminal.physical_jobs,
    };
    for (const [field, sourceField] of Object.entries({ jobs: "physical_jobs", completedJobs: "completed_jobs", poseValidJobs: "pose_valid_jobs", gpuObservedJobs: "gpu_process_observed_jobs", batchWallSeconds: "batch_wall_seconds", cpuSeconds: "worker_cpu_seconds" })) {
      close(numerical[field], readout.numerical.online[sourceField] + readout.numerical.terminal[sourceField], `${id} ${field}`);
    }
    const memory = state.memory ?? [];
    const pendingMemoryCount = memory.filter((entry) => entry.review_state === "pending").length;
    assert.equal(pendingMemoryCount, readout.pending_memory_entries);
    arms.push({
      id, label: ARM_LABELS[id], status: state.status,
      presentationMode: readout.presentation_mode,
      completedAt: readout.campaign_completed_at,
      shortlist: evaluation.shortlist,
      quality, usage, numerical,
      memoryCount: memory.length, pendingMemoryCount,
      source: evaluationPath,
    });
  }

  const metaTokens = metaUsage.usage.input_tokens + metaUsage.usage.output_tokens;
  close(metaTokens, summary.meta.usage.observed_input_plus_output_tokens, "Meta totalTokens");
  assert.equal(binding.release_authorized, summary.meta.release_authorized);
  assert.equal(metaResponse.single_change.component, "task_input_presentation");
  assert.equal(metaResponse.single_change.input_view_mode, "compact_json_v1");
  const result = {
    id: summary.comparison_id,
    title: "Prospective CDK2 comparison",
    target: manifest.target_name,
    assayId: prereg.assay_id,
    generatedAt: summary.generated_at,
    status: summary.status,
    poolSize: prereg.pool_size,
    shortlistSize: prereg.shortlist_size,
    model: prereg.model,
    effort: prereg.effort,
    protocolId: protocol.protocol_id,
    protocolHash: summary.protocol_sha256,
    limits: {
      onlineCandidates: prereg.shared_limits.online_candidates,
      seedsPerCandidate: prereg.shared_limits.seeds_per_candidate,
      batchCandidates: prereg.shared_limits.batch_candidates,
      shortlistSize: prereg.shared_limits.shortlist_size,
      modelCalls: prereg.shared_limits.model_calls_H0_H1,
      modelCallSeconds: prereg.shared_limits.model_call_seconds,
      tokenSoftLimit: prereg.shared_limits.token_soft_limit_H0_H1,
      terminalCandidates: prereg.shared_limits.terminal_candidates,
      terminalFeedback: prereg.shared_limits.terminal_feedback,
    },
    arms, candidates, evidence,
    meta: {
      status: binding.release_authorized ? "release_authorized" : "candidate_not_released",
      releaseAuthorized: binding.release_authorized,
      inputTokens: metaUsage.usage.input_tokens,
      outputTokens: metaUsage.usage.output_tokens,
      totalTokens: metaTokens,
      wallSeconds: metaUsage.wall_seconds,
      presentationMode: metaResponse.single_change.input_view_mode,
      summary: "Meta proposed a single change to task-input presentation: remove JSON whitespace outside strings while preserving the complete logical payload. H1 was evaluated as a candidate; release is not authorized.",
      changes: [
        "Remove formatting whitespace outside JSON strings only.",
        "Preserve values, types, member order, candidate order, arrays, duplicate records, missing values and source references.",
        "Keep the model, tools, protocol, ranking objective, budgets and non-JSON prompt text unchanged.",
        "Retain original input artifacts and record the actual rendered prompt separately.",
      ],
      source: bindingPath,
    },
    audit: {
      passed: audit.audit_passed,
      status: audit.status,
      source: auditPath,
      auditedAt: audit.audited_at_utc,
      crossArmReuseDetected: audit.cross_arm_reuse_detected,
      executionOrder: audit.execution_order_verified,
      firstInputEqualityPassed: audit.first_round_input_comparison.checks_passed,
      memoryReviewStateModified: audit.memory_review_state_modified,
      scope: "Artifact bindings, input replay and execution records passed recorded checks. This is not blinded replication or proof of biological efficacy; scientific memory remains pending review.",
      sourceHashScope: "recorded_binding means this import checked a hash recorded in another source artifact; import_hash_only identifies a local import root without an upstream pinned hash. These checks do not establish independent provenance of the root artifacts. Meta usage also matches the preregistration-bound state call.",
    },
    sources: [...sources].map(([path, hash]) => ({ path, sha256: hash, verification: sourceChecks.get(path) })).sort((a, b) => a.path < b.path ? -1 : a.path > b.path ? 1 : 0),
    limitations: [
      "One pool and one stochastic trajectory per agent arm do not establish significance, generalization or causal improvement.",
      "CNNscore is a GNINA computational ranking signal, not a probability of biological activity. No calibrated affinity pass threshold was established.",
      "The 5/5 paired-valid result describes the final five candidates only; it is distinct from validity across all executed poses and from biological validation.",
      "Execution order was fixed: baseline, H0, then H1. Time drift remains a limitation.",
      "H1 token usage excludes the separate Meta invocation. H1 plus Meta has higher observed token usage than H0 in this experiment; no cost payback is established.",
      "Token counts include observed CLI input and output only. Cached and reasoning subtotals are not added twice; outer development and audit conversations are excluded. USD cost is unknown.",
      "Batch wall time and observed GPU process presence do not measure GPU active time or GPU-hours.",
      "Public historical IC50 values were visible to all arms. Calculations do not constitute new wet-lab measurements.",
      "No result is shown for an uncomputed candidate-arm pair. A missing result is not a zero score or a failed molecule.",
      "H1 remains an unreleased candidate. The recorded audit does not authorize deployment or erase pending scientific-memory review states.",
      "The assay has a homologous single-protein target assignment; receptor and assay context limit biological interpretation.",
      "This portable snapshot is a readout of completed artifacts, not a live connection to agents or GPU workers.",
    ],
  };
  assert.equal(result.meta.totalTokens + arms.find((arm) => arm.id === "H1").usage.totalTokens, summary.descriptive_differences.C_meta_plus_H1_observed_tokens);
  const encoded = JSON.stringify(result);
  assert.ok(!/[\u3400-\u9fff]/u.test(encoded), "Presentation snapshot must be English");
  assert.ok(!encoded.includes("/Users/") && !encoded.includes("/content/") && !encoded.includes("/home/"), "Snapshot must not contain absolute user paths");
  return result;
}

export function renderExperiment(experiment) {
  return `// Generated from immutable experiment artifacts by scripts/import-experiment.mjs.\n// This is a completed-run readout, not live telemetry. Do not edit by hand.\nexport const experiment = ${JSON.stringify(experiment, null, 2)};\n`;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const experiment = await buildExperiment();
  await writeFile(resolve(DEFAULT_ROOT, "frontend/src/experiment-data.js"), renderExperiment(experiment));
  console.log(`Imported ${experiment.candidates.length} candidates, ${experiment.evidence.length} evidence records and ${experiment.sources.length} source hashes.`);
}
