import test from "node:test";
import assert from "node:assert/strict";
import { readFile, mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { resolve } from "node:path";
import { spawn, execFile } from "node:child_process";
import { promisify } from "node:util";
import { createHash } from "node:crypto";
import { handleTraceRequest } from "../trace-server.mjs";

const exec = promisify(execFile);
const reader = new URL("../scripts/read-agent-trace.py", import.meta.url).pathname;
const python = process.env.RSI_LAB_PYTHON || "python3";
const fixtureCode = String.raw`
import sys,json,sqlite3,hashlib
from pathlib import Path
root=Path(sys.argv[1]);(root/'objects').mkdir()
def blob(v):
 raw=json.dumps(v,separators=(',',':')).encode();h=hashlib.sha256(raw).hexdigest();(root/'objects'/h).write_bytes(raw);return h
d=sqlite3.connect(root/'ledger.sqlite3');d.execute('PRAGMA journal_mode=WAL')
d.execute('CREATE TABLE runs(id TEXT,config TEXT,config_hash TEXT)');d.execute('INSERT INTO runs VALUES(?,?,?)',('fixture-only','{}','fixture'))
d.execute('CREATE TABLE events(run_id TEXT,seq INTEGER,event_type TEXT,event_hash TEXT,payload_ref TEXT,created_at REAL)')
d.execute('INSERT INTO events VALUES(?,?,?,?,?,?)',('fixture-only',1,'fixture_event','fixture',blob({}),1793950000))
for k in ['effect','proposal','registration','fact','issue','wait','budget']:
 d.execute('CREATE TABLE v2_'+k+'(run_id TEXT,id TEXT,document TEXT)')
def row(k,v):d.execute('INSERT INTO v2_'+k+' VALUES(?,?,?)',('fixture-only',v['id'],json.dumps(v)))
mid='effect_'+'1'*24;eid='effect_'+'2'*24;tid='effect_'+'3'*24
def effect(i,kind,packet,receipt,metadata,status,costs,holds,unknown):
 row('effect',{'id':i,'kind':kind,'packet_ref':blob(packet),'result_ref':blob(receipt),'metadata':metadata,'created_seq':1,'updated_seq':1,'status':status,'actual_costs':costs,'held_reservations':holds,'accounting_unknown':unknown})
effect(mid,'meta',{'prompt':'Fixture input only <script>alert(1)</script>','model':'fixture'}, {'response':{'command':{'action':'request_capability'}},'usage':{'input_tokens':7,'output_tokens':3}}, {},'succeeded',{'tokens':10,'model_calls':1,'wall_seconds':1},{},False)
effect(eid,'engineering_model',{'prompt':'Unknown fixture input','model':'fixture'},{'response':None,'error':'Fixture remote status unknown'}, {'build_intent_id':'fixture-intent'},'unknown',{'model_calls':1,'wall_seconds':600},{'tokens':25},True)
effect(tid,'sensitivity_host_command',{'argv':['fixture-no-execution'],'name':'fixture-tool'}, {'result':{'ok':True},'stdout':'x'*25000}, {'capability_build_id':'fixture-intent'},'succeeded',{'tool_calls':3,'cpu_seconds':0.1,'wall_seconds':0.2},{},False)
row('proposal',{'id':'fixture-proposal','effect_id':mid,'status':'applied','command':{'action':'request_capability'},'result_ref':blob({'build_intent_id':'fixture-intent'})})
d.commit()
if len(sys.argv)>2 and sys.argv[2]=='hold':
 print('READY',flush=True);sys.stdin.readline()
d.close()
`;

async function fixture({ wal = false } = {}) {
  const root = await mkdtemp(resolve(tmpdir(), "agent-trace-fixture-"));
  let child;
  if (wal) {
    child = spawn(python, ["-u", "-c", fixtureCode, root, "hold"], { stdio: ["pipe", "pipe", "pipe"] });
    await new Promise((yes, no) => {
      child.stdout.once("data", (b) => b.toString().includes("READY") ? yes() : no(new Error("fixture startup")));
      child.once("error", no);
      child.once("exit", (code) => code && no(new Error("fixture exit")));
    });
  } else await exec(python, ["-c", fixtureCode, root]);
  return { root, async close() { if (child) { child.stdin.end("done\n"); await new Promise((yes) => child.once("exit", yes)); } await rm(root, { recursive: true, force: true }); } };
}
async function project(root, args = []) {
  const { stdout } = await exec(python, [reader, "--run", root, ...args], { maxBuffer: 4 * 1024 * 1024 });
  return JSON.parse(stdout);
}

async function addOrchestrationFixture(root) {
  await exec(python, ["-c", String.raw`
import sys,json,sqlite3,hashlib
from pathlib import Path
r=Path(sys.argv[1]);d=sqlite3.connect(r/'ledger.sqlite3')
def blob(v):
 raw=json.dumps(v,separators=(',',':')).encode();h=hashlib.sha256(raw).hexdigest();(r/'objects'/h).write_bytes(raw);return h
for k in ['work','dependency','question']:
 d.execute('CREATE TABLE v2_'+k+'(run_id TEXT,id TEXT,document TEXT)')
def row(k,v):
 v.update(created_seq=1,updated_seq=1);d.execute('INSERT INTO v2_'+k+' VALUES(?,?,?)',('fixture-only',v['id'],json.dumps(v)))
d.execute('INSERT INTO events VALUES(?,?,?,?,?,?)',('fixture-only',2,'work_enqueued','fixture-latest',blob({}),1793950010))
d.execute('UPDATE runs SET config=?',(json.dumps({'goal':'Fixture goal only'}),))
plan={'task_id':'fixture-plan','goal':'Fixture goal only','items':[{'item_id':'one'}],'arms':[{'arm_id':'fixture'}],'metrics':[],'protocol':{'version':'fixture-v1'}}
ref=blob(plan);row('registration',{'id':'task_plan:fixture','kind':'task_plan','status':'frozen','plan_ref':ref,'plan_hash':ref})
row('registration',{'id':'fixture-intent','kind':'capability_build_request','status':'requested','proposal':{'capability_id':'fixture-tool','purpose':'Deliver and validate the requested fixture capability','acceptance_checks':['Fixture check only']}})
row('registration',{'id':'fixture-checkpoint','kind':'driver_checkpoint','status':'quiescent','result_ref':blob({'status':'quiescent','reason':'Fixture historical checkpoint','steps':[]})})
row('work',{'id':'meta-ready','kind':'meta_disposition','status':'ready','issue_id':'fixture-issue','inputs':{}})
row('work',{'id':'build-ready','kind':'capability_build','status':'ready','inputs':{'issue_id':'fixture-issue','build_intent_id':'fixture-intent'}})
row('work',{'id':'old-blocked','kind':'capability_build','status':'blocked','inputs':{}})
row('work',{'id':'finished','kind':'capability_build','status':'succeeded','inputs':{}})
row('dependency',{'id':'fixture-intent','producer':'engineering_executor','revision':1,'value':{'status':'requested'},'content_hash':'fixture-dependency'})
for status in ['waiting','disposed','fired']:
 row('wait',{'id':'wait-'+status,'status':status,'issue_id':'fixture-issue','dependency_id':'fixture-intent','producer':'engineering_executor','observed_version':1,'wake_condition':{'kind':'version_changed'},'deadline_at':1793940000,'expiry_action':'ask_user','disposition_attempted':status=='disposed'})
row('issue',{'id':'fixture-issue','status':'open','scope':'Fixture scope','hypothesis':'Fixture hypothesis, not a conclusion','evidence_ids':['fixture-evidence']})
d.commit();d.close()
`, root]);
}

test("synthetic snapshot exports consistent index and call details", async () => {
  const f = await fixture();
  try {
    const output = resolve(f.root, "export");
    await project(f.root, ["--mode", "recorded", "--export", output]);
    const data = JSON.parse(await readFile(resolve(output, "index.json"), "utf8"));
    assert.equal(data.run.source, "recorded");
    assert.equal(data.run.id, "fixture-only");
    assert.equal(data.run.heartbeat, false);
    assert.equal(data.calls.length, data.summary.effects);
    assert.equal(data.calls.filter(c => c.category === "model").length, data.summary.modelCalls);
    assert.equal(data.calls.reduce((n,c) => n + (c.usage.tokens ?? 0), 0), data.summary.knownTokens);
    assert.equal(data.summary.reservedUnknownTokens, 25);
    for (const call of data.calls) {
      const detail = JSON.parse(await readFile(resolve(output, "calls", `${call.id}.json`), "utf8"));
      assert.equal(detail.id, call.id);
      assert.equal(detail.usage.tokens, call.usage.tokens);
      if (call.category === "model") assert.equal(detail.messages[0].truncated, false);
    }
  } finally { await f.close(); }
});

test("reader snapshots a live WAL without changing source files or dropping committed rows", async () => {
  const f = await fixture({ wal: true });
  try {
    const paths = [resolve(f.root, "ledger.sqlite3"), resolve(f.root, "ledger.sqlite3-wal")];
    const hashes = await Promise.all(paths.map(async (p) => createHash("sha256").update(await readFile(p)).digest("hex")));
    const data = await project(f.root);
    assert.equal(data.calls.length, 3);
    assert.equal(data.summary.knownTokens, 10);
    assert.equal(data.summary.reservedUnknownTokens, 25);
    assert.equal(data.summary.toolEffects, 1);
    assert.equal(data.summary.toolCalls, 3);
    assert.equal(data.labOrchestration.status, "unknown");
    assert.equal(data.labOrchestration.projectionComplete, false);
    assert.deepEqual(await Promise.all(paths.map(async (p) => createHash("sha256").update(await readFile(p)).digest("hex"))), hashes);
  } finally { await f.close(); }
});

test("orchestration separates saved engineering readiness, scientific issues and unknown effects", async () => {
  const f = await fixture();
  try {
    await addOrchestrationFixture(f.root);
    const data = await project(f.root), state = data.labOrchestration;
    assert.equal(state.schemaVersion, "lab_orchestration_v1");
    assert.equal(state.status, "awaiting_capability");
    assert.equal(state.heartbeat, false);
    assert.equal(state.task.objective, "Fixture goal only");
    assert.equal(state.task.plans[0].itemCount, 1);
    assert.equal(state.task.plans[0].planHash, state.task.plans[0].planRef);
    assert.deepEqual(state.readiness.execution.readyWorkByKind, { meta_disposition: 1, capability_build: 1 });
    assert.equal(state.readiness.execution.labReadyWorkCount, 0);
    assert.equal(state.readiness.execution.vinaReadyWorkCount, 0);
    assert.equal(state.readiness.execution.automaticResumeConfirmed, false);
    assert.equal(state.counts.pendingWorks, 3);
    assert.equal(state.readiness.scientific.status, "unresolved");
    assert.deepEqual(state.issues[0].evidenceIds, ["fixture-evidence"]);
    assert.equal(state.unknownEffects.length, 1);
    assert.equal(state.unknownEffects[0].heldReservations.tokens, 25);
    assert.equal(state.unknownEffects[0].actualCosts.tokens, undefined);
    assert.equal(state.unknownEffects[0].packetRef.length, 64);
    assert.ok(state.resumeRequirements.some((r) => r.workIds.includes("build-ready")));
    assert.ok(state.resumeRequirements.some((r) => r.effectIds.includes("effect_" + "2".repeat(24))));
    assert.equal(state.latestDriverCheckpoint.stale, true);
    assert.equal(state.latestDriverCheckpoint.status, "quiescent");
    assert.notEqual(state.status, state.latestDriverCheckpoint.status);
    assert.equal(state.snapshotBasis.eventSequence, 2);
    assert.equal(state.pendingWorks[0].provenance.updatedEvent.eventSequence, 1);
    assert.equal(state.recentTimeline.at(-1).eventSequence, 2);
    assert.equal(state.stages.find((s) => s.id === "output").status, "not_assessed");
    const wait = state.activeWaits.find((w) => w.status === "waiting");
    assert.equal(wait.deadlinePassedAtSnapshot, true);
    assert.equal(wait.dispositionAttempted, false); // Reading elapsed time does not tick the runtime.
    assert.equal(wait.dependency.revision, 1);
    assert.equal(state.activeWaits.length, 2); // disposed still owns a wake obligation; fired does not.
    assert.match(state.activeWaitScopeNote, /Disposed/);
  } finally { await f.close(); }
});

test("detail separates exact request links, missing responses and truncated tool output", async () => {
  const f = await fixture();
  try {
    const unknown = await project(f.root, ["--call", "effect_" + "2".repeat(24)]);
    assert.equal(unknown.messages.some((m) => m.role === "assistant"), false);
    assert.equal(unknown.messages[1].role, "system");
    assert.equal(unknown.requesterCallId, "effect_" + "1".repeat(24));
    const tool = await project(f.root, ["--call", "effect_" + "3".repeat(24)]);
    assert.equal(tool.tool.truncation.stdout.truncated, true);
    assert.equal(tool.tool.stdout.length, 24000);
    assert.equal(tool.tool.truncation.stdout.characters, 25000);
    const model = await project(f.root, ["--call", "effect_" + "1".repeat(24)]);
    assert.match(model.messages[0].content, /<script>/); // Data stays text; UI must escape it.
    assert.equal(model.messages[0].transportPromptAvailable, false);
  } finally { await f.close(); }
});

test("artifact endpoint only downloads a hash bound to the selected effect", async () => {
  const f = await fixture();
  const request = async (url, method = "GET") => {
    const response = {};
    await handleTraceRequest({ url, method }, {
      writeHead(status, headers) { response.status = status; response.headers = headers; },
      end(body) { response.body = body; },
    }, { runDir: f.root, python });
    return response;
  };
  try {
    const index = JSON.parse((await request("/api/trace")).body);
    assert.equal(index.run.source, "ledger");
    const call = index.calls[0];
    const detail = JSON.parse((await request(call.detailUrl)).body);
    const art = detail.artifacts[0];
    const downloaded = await request(art.downloadUrl);
    assert.equal(downloaded.status, 200);
    const raw = downloaded.body;
    assert.equal(createHash("sha256").update(raw).digest("hex"), art.sha256);
    assert.equal((await request("/api/trace/calls/../secrets")).status, 400);
    assert.equal((await request("/api/trace", "POST")).status, 405);
    const missing = await request(`/api/trace/artifacts/${call.id}/${"0".repeat(64)}`);
    assert.equal(missing.status, 503);
    assert.equal(missing.body.includes(f.root), false);
    assert.equal((await request("/api/trace", "HEAD")).body, undefined);
  } finally { await f.close(); }
});
