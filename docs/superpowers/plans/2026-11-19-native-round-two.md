# Native Capabilities Round Two Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a run declare, per role, tool servers started locally or reached by URL, image viewing and a frozen folder of skills that Codex lists itself, kept apart from the owner's everyday Codex, with every tool server call, viewed image and shown skill recorded and every local server process stopped when its call ends.

**Architecture:** `rsi_lab/native.py` learns three optional keys in a role's normal-form settings and everything derived from them: validation, a frozen copy of the skills folder in the run store, the Codex command, the request's short form, the prompt sentences and the receipt form. The executor checks the Codex home, the named environment variables and writes the skills into the session's workspace before each call; the transport tracks a local tool server's processes as it tracks the terminal's and writes them to the call folder for recovery. The stream audit, the rollout reader and `codex_ledger` accept, record and check what the new tools leave behind; the PABLO host passes the keys through and records tool servers as not blind.

**Tech Stack:** Python standard library only in `rsi_lab/`; `unittest`; RDKit in the PABLO host; Codex CLI 0.170.0.

**Spec:** `docs/superpowers/specs/2026-11-18-native-round-two-design.md` (builds on `2026-11-17-native-codex-capabilities-design.md` and `2026-11-18-lab-team-design.md`).

**Before Task 1:** commit this plan: `git add docs/superpowers/plans/2026-11-19-native-round-two.md && git commit -m "Plan tool servers, image viewing and skills for the agents"`.

## Global Constraints

- Codex CLI 0.170.0 only; `transport.SUPPORTED_CODEX_VERSIONS` is unchanged. No task runs `codex`, makes a model call, installs a package or uses the network. New tests that reach `transport.invoke` patch `transport.codex_version` to return `'0.170.0'`.
- `rsi_lab/` imports the standard library only. Kernel tests: `python3 -m unittest discover -s tests` from the worktree root (about 90 s); one module: `python3 -m unittest tests.v2.test_native_settings`.
- PABLO tests: `PYTHONPATH=experiments/pablo:. "$PABLO_PY" -m unittest experiments.pablo.tests.test_team`, where `PABLO_PY` is a local interpreter with RDKit named in the brief and never written into the repository. Tests that need `guacamol` cannot run here and are reported **not run**, never as passing.
- Nothing opens, lists or scans `~/.codex/sessions` or `~/.codex/archived_sessions`. Rollouts are located only through `session_context.find_owned_rollout` or `codex_ledger._rollout_of`. The home check (Task 4) lists the entries of exactly `<Codex home>/skills`, `$HOME/.agents/skills` and `/etc/codex/skills` and records no entry name.
- The repository is public: every id, path, URL, token, variable value and account value in tests and documents is invented (`papers.example.org`, `/data/research-skills`, `/opt/tools/bin/python`, `invented-secret-4f1c`). No fixture copies `creator_user_id`, `creator_account_id` or a real home-directory path.
- A role that sets none of the three keys keeps its stored settings, request bytes, Effect metadata and receipt keys exactly as today. The one deliberate change, for every role without skills including everything off: `-c skills.include_instructions=false` on its command and an after-call check that Codex wrote no skills list. `BASE_PLAIN_PACKET_SHA256` and `BASE_PLAIN_METADATA_SHA256` in `tests/v2/test_native_requests.py` keep passing unedited.
- In normal form `view_image` appears only as `true`, `tool_servers` only when non-empty, `skills` only when declared.
- Tool server: name `[a-z][a-z0-9_]{0,31}`, not beginning with `codex`; at most 8 per role; `description` one line of 1 to 300 characters; `tool_timeout_seconds` 1 to 3,600, optional, no default; exactly one of `command` (absolute path) and `url` (`https://`, or `http://` when the host is `localhost`, `127.0.0.1` or `[::1]`); a header named `Authorization` is refused.
- Skills: name `[a-z][a-z0-9_-]{0,31}`, equal to its sub-folder and to `name` in its `SKILL.md` front matter; 1 to 16 skills; per skill at most 16 files and 262,144 bytes; one-line description of 1 to 300 characters; regular files only, no symbolic links.
- `view_image` and `skills` are accepted only for a role whose terminal is on.
- Secrets travel by name only (`env_from`, `token_from`): their values are never in the run config, a call record, a receipt or a command line. A named variable that is unset or empty refuses the call before Codex starts.
- Command values: per server in name order, local `mcp_servers.<name>.command`, `.args`, `.env`, `.env_vars`; URL `.url`, `.bearer_token_env_var`, `.http_headers`; both `.tool_timeout_sec` when given and `.default_tools_approval_mode="approve"`. A role with skills: `skills.bundled.enabled=false` and `project_root_markers=[]`; a role without: `skills.include_instructions=false`.
- Records keep the first 32 tool server calls and the first 32 viewed image paths; argument excerpts are 200 characters. The name of a skill nobody declared is never recorded or put in an error message.
- The kernel never writes a Codex config, never creates a Codex home, never copies or links credentials.
- Process tracking runs whenever the terminal is on or the role has a local tool server; a URL server alone starts none.

## Review Focus

1. **A sub-agent forked with the lead's history** carries a copy of the lead's skills list, possibly one written before this change listing the owner's personal skills. For a role without skills that copy must not invalidate the call; a list of the sub-agent's own must. Test in Task 9.
2. **How Codex spells a skills root and a skill line**: the workspace through a symlink (macOS `/var` is `/private/var`), a trailing slash, a description holding ` (file: `, prose lines inside "Available skills". Roots are compared resolved and only `- ` lines are parsed. Test in Task 9.
3. **A run prepared before this change, resumed on this code**: the same normal form, request and Effect bytes, no settings mismatch in recovery, and an interrupted everything-off call recorded with the old command can still be isolated. Tests in Tasks 1, 3 and 6.
4. **A secret named by `env_from` or `token_from`**: its value appears in no file of the call folder and no command line; an unset or empty one refuses the call before Codex starts, in the transport and in the executor. Tests in Tasks 3 and 5.
5. **Viewed images that are not small PNGs**: a `data:image/jpeg` URL of several kilobytes is accepted without decoding, counted in the text history and never against the attached-image limit; `file://`, `https://` and `data:text/…` items are refused, as is any image in a role without image viewing. Test in Task 7.

## Decided here (the design left these to the builder)

- Frozen skill files: each file's bytes are the run-store blob `objects/<sha256 of the bytes>` (`Store.put_blob`); the settings name them as `skills.frozen[<name>].files[<relative path>] = <sha256>`.
- Record field names: `tool_server_calls` entries `{server, tool, status, call_id, arguments_sha256, arguments_excerpt, result_bytes, duration_seconds}`; `images_viewed` `{paths, paths_omitted, count, bytes}`; `skills_shown`; `skills_unexpected` `{count, folders}`; the call folder's `tracked_processes.json`.
- Prompt wording and the separate-home refusal: given verbatim in Tasks 6 and 4.
- `tool_timeout_seconds` has no default: when absent nothing is passed and Codex's own default applies.
- The skills list of a resumed thread is looked for only in the part of the rollout the call added.

## File map

| File | Responsibility | Tasks |
|---|---|---|
| `rsi_lab/native.py` | the three keys: validation, freezing, home check, command values, request and receipt forms, prompt, allowed stream and rollout kinds | 1–7 |
| `rsi_lab/__main__.py` | `init` freezes skills and checks the home | 2, 4 |
| `rsi_lab/runtime.py` | home check at start; full settings in the Effect, short form in the request | 4, 6 |
| `rsi_lab/transport.py` | command, variable and home refusals, `write_skills`, stream audit, tracked-process file | 3, 4, 5, 7, 10 |
| `rsi_lab/roles.py` | executor preflight, request-form check, prompt input, `skills_error` | 5, 6, 9 |
| `rsi_lab/session_context.py` | viewed images inside tool outputs | 7 |
| `rsi_lab/codex_ledger.py` | tool server calls, viewed images, skills lists and the after-call check | 8, 9 |
| `rsi_lab/call_recovery.py` | `skills_error`; stopping recorded processes | 9, 10 |
| `rsi_lab/interruption.py` | accepts the command built before this change | 3 |
| `tests/v2/native_fixtures.py` | settings constants and record builders | 1, 2, 7, 9 |
| `experiments/pablo/pablo_task/team.py`, `experiments/pablo/run.py`, `experiments/pablo/TEAM_V1.md` | team runs with the new keys | 11 |
| `docs/persistent-agents.md` (section `## Codex 原生工具`), `README.md` | user documentation, in Chinese like the rest of the file | each task, README in 10 |

---

### Task 1: Tool servers and image viewing in the settings

**Files:**
- Modify: `rsi_lab/native.py` (module docstring, `_role`, `_shape`, `receipt_form`, new helpers), `tests/v2/native_fixtures.py`
- Modify (docs): `docs/persistent-agents.md`: new subsection `### 工具服务与查看图片` after `### 可读目录`
- Test: `tests/v2/test_native_settings.py` (new class `ToolServerSettingsTests`), `tests/v2/test_native_recovery.py`

**Interfaces:**
- Consumes: `capabilities`, `validate`, `any_on`, `_role`, `_shape`, `receipt_form`, the role description rule of `_roles`.
- Produces:
  - Normal form gains optional keys `'view_image': True` and `'tool_servers': {name: server}` (name order). A local server is `{'description', 'command', 'args': [str], 'env': {str: str}, 'env_from': [str]}` plus `'tool_timeout_seconds'` only when given; a URL server is `{'description', 'url', 'headers': {str: str}}` plus `'token_from'` and `'tool_timeout_seconds'` only when given. `env` and `headers` are in key order; `args` and `env_from` keep the given order. `command` is stored as given, never resolved (a virtual environment's interpreter is a symlink that must stay one).
  - `native.OPTIONAL_KEYS = ('view_image', 'tool_servers', 'skills')`; `SERVER_NAME`, `MAX_TOOL_SERVERS = 8`, `MAX_TOOL_TIMEOUT = 3600`, `ENV_NAME = re.compile('[A-Za-z_][A-Za-z0-9_]{0,127}')`, `LOOPBACK_HOSTS = ('localhost', '127.0.0.1', '::1')`.
  - `native.tool_servers(settings) -> dict`, `native.local_servers(settings) -> list[str]`, `native.secret_variables(settings) -> list[str]` (the `env_from` and `token_from` names, sorted, once each), `native.unset_variables(settings, environ) -> list[str]` (those unset or empty in `environ`).
  - `native.receipt_form`: a local server's `env` becomes `env_names`, its keys in order; everything else as in normal form.
  - Fixtures `CHEM`, `PAPERS`, `TOOL_SERVERS`, `WITH_TOOL_SERVERS`, `LOCAL_SERVER_ONLY`, `URL_SERVER_ONLY`, `VIEW_IMAGE`, and `here(server)` = `{**server, 'command': sys.executable}` for tests that go through `capabilities`.

- [ ] **Step 1: Write the failing tests**

```python
CHEM = {'description': 'Fingerprints and similarity for SMILES.', 'command': '/opt/tools/bin/python',
        'args': ['-m', 'chem_server'], 'env': {'CHEM_MODE': 'fast'}, 'env_from': ['CHEM_API_KEY'],
        'tool_timeout_seconds': 300}
PAPERS = {'description': 'Full text of open-access papers.', 'url': 'https://papers.example.org/mcp',
          'headers': {'X-Client': 'rsi-lab'}, 'token_from': 'PAPERS_TOKEN'}
TOOL_SERVERS = {'chem': CHEM, 'papers': PAPERS}
WITH_TOOL_SERVERS = {**SEARCH, 'tool_servers': TOOL_SERVERS}
LOCAL_SERVER_ONLY = {'web_search': False, 'terminal': None, 'subagents': None, 'tool_servers': {'chem': CHEM}}
URL_SERVER_ONLY = {'web_search': False, 'terminal': None, 'subagents': None, 'tool_servers': {'papers': PAPERS}}
VIEW_IMAGE = {**TERMINAL, 'view_image': True}

def lab(entry):
    return native.capabilities({'native_capabilities': {'lab': entry}})['lab']

def test_servers_reach_the_normal_form_in_name_order(self):
    got = lab({'web_search': True, 'tool_servers': {'papers': PAPERS, 'chem': here(CHEM)}})
    self.assertEqual(got, {**SEARCH, 'tool_servers': {'chem': here(CHEM), 'papers': PAPERS}})
    self.assertEqual(list(got['tool_servers']), ['chem', 'papers'])
    self.assertEqual(lab({'tool_servers': {'a': {'description': 'd', 'command': sys.executable}}})['tool_servers']['a'],
                     {'description': 'd', 'command': sys.executable, 'args': [], 'env': {}, 'env_from': []})
    self.assertEqual(lab({'tool_servers': {'b': {'description': 'd', 'url': 'http://[::1]:8080/mcp'}}})['tool_servers']['b'],
                     {'description': 'd', 'url': 'http://[::1]:8080/mcp', 'headers': {}})

def test_keys_that_are_off_leave_the_settings_as_today(self):                  # Review Focus 3
    for extra in ({}, {'view_image': False}, {'tool_servers': {}}):
        self.assertEqual(lab({'terminal': True, **extra}), TERMINAL)
    for settings in (SEARCH, TERMINAL, PLAIN, WITH_ROLES):
        self.assertEqual(native.validate(settings), settings)
    for settings in (SEARCH, TERMINAL, PLAIN):
        self.assertEqual(native.receipt_form(settings), settings)

def test_image_viewing_needs_the_terminal(self):
    self.assertEqual(lab({'terminal': True, 'view_image': True}), VIEW_IMAGE)
    for entry in ({'view_image': True}, {'web_search': True, 'view_image': True}, {'terminal': True, 'view_image': 1}):
        with self.subTest(entry=entry), self.assertRaisesRegex(ValueError, '^native_capabilities.lab.view_image'):
            lab(entry)

def test_bad_servers_are_refused(self):
    ok, url = {'description': 'd', 'command': sys.executable}, {'description': 'd', 'url': PAPERS['url']}
    bad = [{'Chem': ok}, {'codex_x': ok}, {'1x': ok}, {'a' * 33: ok}, {'s%d' % i: ok for i in range(9)}, ['chem'],
           {'x': {**ok, 'url': PAPERS['url']}}, {'x': {'description': 'd'}}, {'x': {**ok, 'command': 'python'}},
           {'x': {**ok, 'command': '/nonexistent/server'}}, {'x': {**ok, 'args': '-m x'}}, {'x': {**ok, 'env': {'A': 1}}},
           {'x': {**ok, 'env': {'1A': 'v'}}}, {'x': {**ok, 'env_from': ['A', 'A']}},
           {'x': {**ok, 'env': {'A': 'v'}, 'env_from': ['A']}}, {'x': {**ok, 'headers': {}}}, {'x': {**url, 'env': {}}},
           {'x': {**ok, 'description': ''}}, {'x': {**ok, 'description': 'a\nb'}}, {'x': {**ok, 'description': 'd' * 301}},
           {'x': {**ok, 'tool_timeout_seconds': 0}}, {'x': {**ok, 'tool_timeout_seconds': 3601}},
           {'x': {**ok, 'tool_timeout_seconds': True}}, {'x': {**url, 'url': 'http://papers.example.org/mcp'}},
           {'x': {**url, 'url': 'ftp://localhost/mcp'}}, {'x': {**url, 'url': 'https://user:pw@papers.example.org/mcp'}},
           {'x': {**url, 'url': 'https:///mcp'}}, {'x': {**url, 'headers': {'Authorization': 'Bearer t'}}},
           {'x': {**url, 'headers': {'authorization': 't'}}}, {'x': {**url, 'headers': {'X-A': 'a\nb'}}},
           {'x': {**url, 'token_from': 'not a name'}}]
    for servers in bad:
        with self.subTest(servers=servers), self.assertRaisesRegex(ValueError, '^native_capabilities.lab.tool_servers'):
            lab({'tool_servers': servers})

def test_loopback_http_and_the_limits_are_accepted(self):
    for host in ('localhost', '127.0.0.1', '[::1]'):
        lab({'tool_servers': {'x': {'description': 'd', 'url': 'http://' + host + ':8080/mcp'}}})
    lab({'tool_servers': {'s%d' % i: {'description': 'd' * 300, 'command': sys.executable,
                                      'tool_timeout_seconds': 3600} for i in range(8)}})

def test_validate_checks_the_shape_only(self):
    self.assertEqual(native.validate(WITH_TOOL_SERVERS), WITH_TOOL_SERVERS)       # the command need not exist
    self.assertEqual(native.validate(VIEW_IMAGE), VIEW_IMAGE)
    self.assertTrue(native.any_on(URL_SERVER_ONLY))
    with self.assertRaises(ValueError):
        native.validate({**SEARCH, 'view_image': True})

def test_secret_names_and_local_servers(self):
    self.assertEqual((native.tool_servers(SEARCH), native.local_servers(WITH_TOOL_SERVERS)), ({}, ['chem']))
    self.assertEqual(native.secret_variables(WITH_TOOL_SERVERS), ['CHEM_API_KEY', 'PAPERS_TOKEN'])
    self.assertEqual(native.unset_variables(WITH_TOOL_SERVERS, {'CHEM_API_KEY': 'x', 'PAPERS_TOKEN': ''}), ['PAPERS_TOKEN'])

def test_receipt_form_keeps_env_names_not_values(self):
    got = native.receipt_form(WITH_TOOL_SERVERS)['tool_servers']
    self.assertEqual(got['chem'], {'description': CHEM['description'], 'command': CHEM['command'], 'args': CHEM['args'],
                                   'env_names': ['CHEM_MODE'], 'env_from': ['CHEM_API_KEY'], 'tool_timeout_seconds': 300})
    self.assertEqual(got['papers'], PAPERS)
```

In `test_native_recovery.py`: `test_recovery_sees_changed_tool_servers`: a call recorded with `WITH_TOOL_SERVERS` (use `TOOL_FREE_STREAM`, `turn=rows('t2')`) is refused with `recovery_binding_mismatch` against an Effect whose `papers` description differs, and recovered against the same settings.

- [ ] **Step 2: Run them and see them fail**

Run: `python3 -m unittest tests.v2.test_native_settings tests.v2.test_native_recovery`
Expected: the new tests fail on the unknown keys; the existing ones pass.

- [ ] **Step 3: Implement in `rsi_lab/native.py`**

`_role` accepts `OFF` keys plus `view_image` and `tool_servers` (and `skills` from Task 2); `_shape` accepts the optional keys only in the forms above. Error messages start with `<where>.view_image` or `<where>.tool_servers`. The URL is checked with `urllib.parse.urlsplit`: scheme `https` with a host, or `http` with `hostname` in `LOOPBACK_HOSTS`; no user name or password. Header names match `[!#$%&'*+.^_`|~0-9A-Za-z-]{1,64}`, values are 1 to 1,024 printable ASCII characters. `capabilities` alone also requires each `command` to be an existing executable regular file (`os.path.isfile` and `os.access(..., os.X_OK)`), as it requires readable roots to exist.

- [ ] **Step 4: Document** in the new subsection: the JSON example of the design's section 1; the two kinds and their keys; that only variable names are stored; that a named variable unset at call time refuses the call; that every result is a lead; what Codex sends a server with each call (thread and session ids, model, Codex version and, when the run folder lies inside a git repository, that repository's local path, remote URL and latest commit), and the advice to keep the run folder outside any repository when a server belongs to someone else; that a URL server is a way out like web search, so private data and credentials must not be readable by that role; that `view_image` needs the terminal.

- [ ] **Step 5: Run and commit**

Run: `python3 -m unittest discover -s tests`
Expected: all pass, no existing test edited.

```bash
git add rsi_lab/native.py tests/v2 docs/persistent-agents.md
git commit -m "Accept tool servers and image viewing in the native settings"
```

---

### Task 2: Declaring and freezing a skills folder

**Files:**
- Modify: `rsi_lab/native.py`, `rsi_lab/__main__.py` (`init`), `tests/v2/native_fixtures.py`
- Modify (docs): `docs/persistent-agents.md`: new subsection `### 技能` after `### 工具服务与查看图片`
- Test: `tests/v2/test_native_settings.py` (new class `SkillSettingsTests`)

**Interfaces:**
- Consumes: Task 1's `_shape` and `OPTIONAL_KEYS`; `Store.put_blob`; `store.digest`.
- Produces:
  - Config form `"skills": {"root": "<absolute folder>"}`. Normal (frozen) form `'skills': {'root': <resolved path>, 'frozen': {name: {'description': str, 'files': {relative path: sha256}}}}`, skills in name order, files in path order. `capabilities` and `validate` refuse an entry without `frozen` with a message containing `must be frozen with the run`.
  - `native.SKILL_NAME = re.compile('[a-z][a-z0-9_-]{0,31}')`, `MAX_SKILLS = 16`, `MAX_SKILL_FILES = 16`, `MAX_SKILL_BYTES = 262144`.
  - `native.read_skills(root) -> {name: {'description': str, 'files': {relative path: bytes}}}` (raises `ValueError`).
  - `native.freeze_skills(block, put=None) -> dict`: `block` is a config's `native_capabilities` object; returns a new block in which every role's `skills` is frozen. `put(raw) -> sha256` stores the bytes; `None` freezes without storing. An entry already frozen is read again from its `root` and must give the same `frozen`, else `ValueError` containing `changed since it was frozen`. A non-object block raises the message `capabilities` raises; other malformed entries are left for `capabilities`.
  - `native.declared_skills(settings) -> dict` (the `frozen` mapping, `{}` when none).
  - `native.receipt_form`: `skills` becomes `{name: store.digest(files mapping)}`.
  - Fixtures: `SKILL_MD`, `CHECK_PY`, `PLOT_CHECK`, `WITH_SKILLS`, `skill_folder(root)`.

**Folder rules.** `root` is an absolute existing directory, stored resolved. Every entry whose name begins with `.` is skipped at every level (decided here: a Finder `.DS_Store` must not refuse a run). The other top-level entries are directories named by `SKILL_NAME`; each holds `SKILL.md`. Regular files only; a symbolic link anywhere below the root refuses. `SKILL.md` starts with a line `---` and its front matter ends at the next line `---`; `name:` and `description:` lines are read as `key: value`, the value stripped and one pair of matching surrounding quotes removed; `name` equals the folder name; the description is one line of 1 to 300 characters that does not begin with `|` or `>` (YAML block text). Errors start with `native_capabilities.<role>.skills`. `_shape` checks a frozen form against the same names, counts and description rule, that each skill's files include `SKILL.md`, that each path is relative with no empty, `.` or `..` part and no part beginning with `.`, and that each hash is 64 lowercase hex characters; sizes are checked only when the folder is read.

- [ ] **Step 1: Write the failing tests**

```python
SKILL_MD = (b'---\nname: plot-check\ndescription: Checks a plot before it is reported.\n---\n'
            b'Open the plot and check its axes and units.\n')
CHECK_PY = b'print("axes checked")\n'
PLOT_CHECK = {'description': 'Checks a plot before it is reported.',
              'files': {'SKILL.md': hashlib.sha256(SKILL_MD).hexdigest(),
                        'scripts/check.py': hashlib.sha256(CHECK_PY).hexdigest()}}
WITH_SKILLS = {**TERMINAL, 'skills': {'root': '/data/research-skills', 'frozen': {'plot-check': PLOT_CHECK}}}

def test_freezing_stores_every_file_and_records_its_hash(self):
    root, stored = skill_folder(self.tmp / 'skills'), {}
    def put(raw):
        stored[hashlib.sha256(raw).hexdigest()] = raw
        return hashlib.sha256(raw).hexdigest()
    block = native.freeze_skills({'lab': {'terminal': True, 'skills': {'root': str(root)}}}, put)
    self.assertEqual(block['lab']['skills'], {'root': str(root.resolve()), 'frozen': {'plot-check': PLOT_CHECK}})
    self.assertEqual(sorted(stored.values()), sorted([SKILL_MD, CHECK_PY]))
    self.assertEqual(native.capabilities({'native_capabilities': block})['lab'], {**TERMINAL, 'skills': block['lab']['skills']})

def test_later_changes_to_the_folder_do_not_reach_the_run(self):
    # freeze, then rewrite scripts/check.py: capabilities of the frozen block is unchanged, and
    # freeze_skills of the frozen block raises ValueError matching 'changed since it was frozen'

def test_the_kernel_refuses_an_unfrozen_entry_and_skills_without_a_terminal(self):
    # {'terminal': True, 'skills': {'root': ...}} -> '^native_capabilities.lab.skills.*must be frozen with the run'
    # freeze_skills of {'web_search': True, 'skills': {'root': ...}} then capabilities -> '^native_capabilities.lab.skills'

def test_folder_limits_refuse_the_run(self):
    # each case raises ValueError matching '^native_capabilities.lab.skills' from freeze_skills or capabilities:
    # an empty folder; 17 skills; 17 files in one skill; 262,145 bytes in one skill; a folder named Plot;
    # front matter name 'plot' in folder plot-check; no SKILL.md; no front matter; a blank description;
    # 'description: >'; a 301-character description; a symlinked file; a symlinked skill folder;
    # a regular file at the top level; a relative or missing root

def test_the_limits_themselves_are_accepted(self):
    # 16 skills; one skill of 16 files totalling 262,144 bytes; a quoted description '"Checks: axes."' read as
    # 'Checks: axes.'; .DS_Store at the top level and inside a skill, absent from 'files'

def test_init_freezes_the_folder_into_the_run(self):
    # HOME and CODEX_HOME patched to empty temporary folders (Task 4 adds the home check to init).
    # main([... 'init', '--config', path]) with {'budgets': BUDGETS, 'native_capabilities':
    #   {'lab': {'terminal': True, 'skills': {'root': str(root)}}}}
    # then store.run('r')['config']['native_capabilities']['lab']['skills']['frozen'] == {'plot-check': PLOT_CHECK}
    # and store.read_blob(PLOT_CHECK['files'][path]) gives each file's bytes

def test_receipt_form_names_skills_by_hash(self):
    self.assertEqual(native.receipt_form(WITH_SKILLS)['skills'], {'plot-check': digest(PLOT_CHECK['files'])})
```

- [ ] **Step 2: Run them and see them fail**

Run: `python3 -m unittest tests.v2.test_native_settings -k SkillSettings`

- [ ] **Step 3: Implement** the folder rules in `read_skills`, `freeze_skills`, the frozen shape in `_shape`, `declared_skills` and the `receipt_form` change. In `__main__.py`'s `init`, before the existing checks: `config['native_capabilities'] = native.freeze_skills(config['native_capabilities'], store.put_blob)` when the key is present.

- [ ] **Step 4: Document** in `### 技能`: the folder layout, the front matter, the limits, that hidden entries are skipped, that `init` reads the folder once and later changes do not reach the run, that skills need the terminal.

- [ ] **Step 5: Run and commit**

Run: `python3 -m unittest discover -s tests`

```bash
git add rsi_lab/native.py rsi_lab/__main__.py tests/v2 docs/persistent-agents.md
git commit -m "Freeze a declared skills folder into the run store"
```

---

### Task 3: The Codex command

**Files:**
- Modify: `rsi_lab/native.py` (`command_parts`, `check_roots`, new `toml_array`, `everyday_codex_home`), `rsi_lab/transport.py` (`_command`, `invoke`), `rsi_lab/interruption.py` (the command comparison at line 166)
- Modify (docs): `docs/persistent-agents.md`: `### Codex 命令的变化`, `### 可读目录`, `### 恢复与隔离`, `### 尚不支持`
- Test: `tests/v2/test_native_transport.py`, `tests/v2/test_native_settings.py`, `tests/v2/test_interrupted_model_quarantine.py`

**Interfaces:**
- Consumes: Tasks 1 and 2's normal form, `tool_servers`, `declared_skills`, `unset_variables`.
- Produces:
  - `native.toml_array(items: list[str]) -> str`; `native.everyday_codex_home() -> Path` (`Path.home() / '.codex'`, read at each call).
  - `command_parts(...)`, signature unchanged. `config` order: terminal values, sub-agent values, tool server values, skills values. With the terminal on, `(everyday home, 'deny')` follows the Codex home's entry when the everyday home exists as a directory and differs from the call's Codex home (both resolved). `enabled` gains `view_image` when it is on.
  - `transport._command`: `'-c', 'skills.include_instructions=false'` right after the `agents.enabled=false` pair (after `suppress_unstable_features_warning=true` when sub-agents are on) whenever no skills are declared.
  - `transport.invoke` raises `ValueError('Native settings name environment variables that are not set: <names>')` before anything is written or Codex is asked for its version.
  - `check_roots` also refuses a readable root equal to or inside `everyday_codex_home()`, naming `the everyday Codex home`.
  - `certify_interruption` accepts a recorded command equal to the rebuilt one, or to the rebuilt one without its `'-c', 'skills.include_instructions=false'` pair (a call recorded before this change).

- [ ] **Step 1: Write the failing tests**

In `NativeTransportTests.setUp`, patch `HOME` to an empty temporary folder so the existing exact-table tests stay as they are.

```python
TODAY = [..., '-c', 'suppress_unstable_features_warning=true', '-c', 'agents.enabled=false',
         '-c', 'skills.include_instructions=false']                        # the existing constant, one pair longer
SERVER_VALUES = [
    'mcp_servers.chem.command="/opt/tools/bin/python"', 'mcp_servers.chem.args=["-m","chem_server"]',
    'mcp_servers.chem.env={"CHEM_MODE"="fast"}', 'mcp_servers.chem.env_vars=["CHEM_API_KEY"]',
    'mcp_servers.chem.tool_timeout_sec=300', 'mcp_servers.chem.default_tools_approval_mode="approve"',
    'mcp_servers.papers.url="https://papers.example.org/mcp"', 'mcp_servers.papers.bearer_token_env_var="PAPERS_TOKEN"',
    'mcp_servers.papers.http_headers={"X-Client"="rsi-lab"}', 'mcp_servers.papers.default_tools_approval_mode="approve"']

def parts(self, settings, codex_home='/h'):
    return native.command_parts(settings, workspace='/w', run_root='/r', codex_home=codex_home, model='m', effort='low')

def test_tool_servers_are_named_in_name_order(self):
    self.assertEqual(self.parts(WITH_TOOL_SERVERS)['config'], SERVER_VALUES)
    self.assertEqual(self.parts(WITH_TOOL_SERVERS)['sandbox'], ['--sandbox', 'read-only'])
    command = self.command(WITH_TOOL_SERVERS)
    for value in SERVER_VALUES + ['skills.include_instructions=false']:
        self.assertEqual(command[command.index(value) - 1], '-c')

def test_values_not_given_are_left_out(self):
    bare = {'web_search': False, 'terminal': None, 'subagents': None, 'tool_servers': {
        'a': {'description': 'd', 'command': '/bin/s', 'args': [], 'env': {}, 'env_from': []},
        'b': {'description': 'd', 'url': 'http://localhost:9/mcp', 'headers': {}}}}
    self.assertEqual(self.parts(bare)['config'], [
        'mcp_servers.a.command="/bin/s"', 'mcp_servers.a.default_tools_approval_mode="approve"',
        'mcp_servers.b.url="http://localhost:9/mcp"', 'mcp_servers.b.default_tools_approval_mode="approve"'])

def test_image_viewing_is_no_longer_disabled(self):
    command = self.command(VIEW_IMAGE)
    self.assertNotIn('view_image', command)
    self.assertEqual(command[command.index('image_generation') - 1], '--disable')

def test_skills_settings_on_the_command(self):
    self.assertEqual(self.parts(WITH_SKILLS)['config'][-2:], ['skills.bundled.enabled=false', 'project_root_markers=[]'])
    command = self.command(WITH_SKILLS)
    self.assertNotIn('skills.include_instructions=false', command)
    moved = {**WITH_SKILLS, 'skills': {**WITH_SKILLS['skills'], 'root': '/elsewhere'}}
    self.assertEqual(self.command(moved), command)                         # nothing depends on the owner's folders
    for settings in (None, SEARCH, TERMINAL, PLAIN, WITH_TOOL_SERVERS):
        self.assertIn('skills.include_instructions=false', self.command(settings))

def test_a_separate_home_also_denies_the_everyday_one(self):
    home = self.root / 'user'; (home / '.codex').mkdir(parents=True)
    everyday = str((home / '.codex').resolve())
    with patch.dict(os.environ, {'HOME': str(home)}):
        table = tomllib.loads('t = ' + self.config_value(self.command(TERMINAL), 'permissions.rsi_agent.filesystem='))['t']
        self.assertEqual(list(table)[2:4], ['/home/.codex', everyday]); self.assertEqual(table[everyday], 'deny')
        same = self.parts(TERMINAL, codex_home=everyday)['config']
        self.assertEqual(sum(value.count(json.dumps(everyday)) for value in same), 1)
    with patch.dict(os.environ, {'HOME': str(self.root / 'nobody')}):
        self.assertNotIn('nobody', ''.join(self.command(TERMINAL)))

def test_secret_values_reach_no_record_and_no_command(self):                  # Review Focus 4
    secret = 'invented-secret-4f1c'
    with patch.dict(os.environ, {'CHEM_API_KEY': secret, 'PAPERS_TOKEN': secret + '-url'}), \
            patch.object(transport, 'codex_version', return_value='0.170.0'):
        self.invoke(native={**LOCAL_SERVER_ONLY, 'tool_servers': TOOL_SERVERS})
    for path in self.folder.rglob('*'):
        if path.is_file():
            self.assertNotIn(secret.encode(), path.read_bytes(), path.name)
    self.assertFalse(any(secret in part for part in self.commands[0]))

def test_an_unset_or_empty_variable_refuses_before_codex_starts(self):        # Review Focus 4
    for token in (None, ''):
        with self.subTest(token=token), patch.dict(os.environ, {'CHEM_API_KEY': 'x'}), \
                patch.object(transport, 'codex_version', side_effect=AssertionError('asked')):
            os.environ.pop('PAPERS_TOKEN', None)
            if token is not None:
                os.environ['PAPERS_TOKEN'] = token
            with self.assertRaisesRegex(ValueError, 'not set: PAPERS_TOKEN'):
                self.invoke(native=WITH_TOOL_SERVERS)
            self.assertEqual(self.commands, []); self.assertFalse(self.folder.exists())
```

In `test_native_settings.py`, `test_a_readable_root_in_the_everyday_home_is_refused`: with `HOME` patched to a folder holding `.codex/memories`, `check_roots` with that root and `codex_home=self.codex_home` raises matching `'^native_capabilities.lab.terminal.readable_roots.*the everyday Codex home'`.

In `test_interrupted_model_quarantine.py`, `test_a_call_recorded_before_the_skills_switch_can_still_be_isolated` (Review Focus 3): rewrite the recorded command as `_command(...)` without the `'-c', 'skills.include_instructions=false'` pair; certification succeeds. Removing `'-c', 'agents.enabled=false'` instead is still refused with `Only the exact tool-disabled local Codex command can be isolated`.

- [ ] **Step 2: Run them and see them fail**

Run: `python3 -m unittest tests.v2.test_native_transport tests.v2.test_native_settings tests.v2.test_interrupted_model_quarantine`

- [ ] **Step 3: Implement** as listed under Interfaces; `toml_array` is `'[' + ','.join(toml_string(item) ...) + ']'`; `tool_timeout_sec` is a bare integer.

- [ ] **Step 4: Document** the new command values, the skills switch on every role without skills, the denial of `~/.codex` under a separate home and the refused readable root inside it, that isolation accepts a call recorded with the command of before this change, and in `### 尚不支持` replace "外部 MCP 服务器" with plugins, browser and desktop control, account connectors, image generation and user hooks.

- [ ] **Step 5: Run and commit**

Run: `python3 -m unittest discover -s tests`

```bash
git add rsi_lab/native.py rsi_lab/transport.py rsi_lab/interruption.py tests/v2 docs/persistent-agents.md
git commit -m "Name tool servers, image viewing and skills on the Codex command"
```

---

### Task 4: A Codex home without skills of its own

**Files:**
- Modify: `rsi_lab/native.py`, `rsi_lab/runtime.py` (`Runtime.__init__`, after `check_roots`), `rsi_lab/__main__.py` (`init`, after `check_roots`), `rsi_lab/transport.py` (`invoke`, right after `_session_options`)
- Modify (docs): `docs/persistent-agents.md`: new subsection `### 独立的 Codex 主目录` after `### 技能`
- Test: `tests/v2/test_native_settings.py` (new class `SkillHomeTests`), `tests/v2/test_native_transport.py`

**Interfaces:**
- Consumes: `declared_skills`, `default_codex_home`.
- Produces:
  - `native.MACHINE_SKILLS = Path('/etc/codex/skills')` (tests patch it).
  - `native.SEPARATE_HOME_HELP = ('Create an empty folder, run codex login once with CODEX_HOME set to it, and start the Controller or the host with CODEX_HOME set to it. ~/.agents/skills must be empty whatever the Codex home is.')`
  - `native.check_skill_homes(codex_home) -> None`: raises `ValueError('A role with skills needs a separate Codex home with no skills of its own: ' + folder + ' holds entries. ' + SEPARATE_HOME_HELP)` for the first of `<codex_home>/skills` (its `.system` entry not counted), `<HOME>/.agents/skills`, `MACHINE_SKILLS` that holds any entry, or that exists and is not a directory. A missing folder passes.
  - Called only when a role, or the call's settings, declare skills: by `init` and `Runtime.__init__` with `native.default_codex_home()`, by `invoke` with the session's resolved Codex home, raising before Codex is asked for its version.

- [ ] **Step 1: Write the failing tests**

```python
def test_each_of_the_three_folders_refuses_and_no_name_is_told(self):
    # HOME patched to self.user, MACHINE_SKILLS patched to self.machine; one case each:
    # self.codex_home/'skills'/'secret-skill', self.user/'.agents'/'skills'/'secret-skill', self.machine/'secret-skill'
    with self.assertRaisesRegex(ValueError, 'separate Codex home') as raised:
        native.check_skill_homes(self.codex_home)
    self.assertNotIn('secret-skill', str(raised.exception))

def test_the_system_folder_and_missing_folders_pass(self):
    (self.codex_home / 'skills' / '.system' / 'imagegen').mkdir(parents=True)
    native.check_skill_homes(self.codex_home)

def test_a_file_where_a_skills_folder_should_be_is_refused(self):
    (self.user / '.agents').mkdir(parents=True); (self.user / '.agents' / 'skills').write_text('x')
    with self.assertRaisesRegex(ValueError, 'separate Codex home'):
        native.check_skill_homes(self.codex_home)

def test_init_start_and_call_check_the_home_only_for_skills(self):
    # with self.codex_home/skills/x present: init of a WITH_SKILLS config raises and creates no run;
    # a run initialised before the folder appeared refuses Runtime(store, 'r');
    # transport.invoke with WITH_SKILLS and a session whose codex_home is self.codex_home raises before
    #   Codex starts (subprocess.Popen patched to raise AssertionError);
    # a config with only TERMINAL initialises and starts with the same folder full.
```

- [ ] **Step 2: Run them and see them fail**

Run: `python3 -m unittest tests.v2.test_native_settings -k SkillHome` and `tests.v2.test_native_transport`

- [ ] **Step 3: Implement** `check_skill_homes` with `os.scandir` on each folder (entries only, never their contents) and the three call sites.

- [ ] **Step 4: Document** why a role with skills needs its own Codex home, how to make one (`CODEX_HOME=<folder> codex login` once, then start with `CODEX_HOME` set), that the kernel never creates it or copies credentials, that a run stays with the home it started in, that any run may use it, the `$HOME/.agents/skills` limit, and that every start, `recover` included, is refused while one of the folders holds an entry.

- [ ] **Step 5: Run and commit**

Run: `python3 -m unittest discover -s tests`

```bash
git add rsi_lab/native.py rsi_lab/runtime.py rsi_lab/__main__.py rsi_lab/transport.py tests/v2 docs/persistent-agents.md
git commit -m "Refuse a skills call in a Codex home that has skills of its own"
```

---

### Task 5: Skills written into the workspace, and the executor's refusals

**Files:**
- Modify: `rsi_lab/transport.py` (new `write_skills`), `rsi_lab/roles.py` (`CodexExecutor.__call__`, the persistent-session preflight at lines 861–912)
- Modify (docs): `docs/persistent-agents.md`: `### 技能` and `### 终端调用前后的检查`
- Test: `tests/v2/test_native_transport.py`, `tests/v2/test_native_executor.py`

**Interfaces:**
- Consumes: Task 2's `declared_skills` and fixtures; Task 4's `check_skill_homes`; Task 1's `unset_variables`.
- Produces:
  - `transport.write_skills(workspace: Path, skills: dict, objects: Path) -> None`: `skills` is `native.declared_skills(settings)`. Every blob `objects/<sha256>` is read first and must be a regular file, not a symlink, whose SHA-256 is its name; otherwise `ValueError` and nothing is deleted or written. Then `<workspace>/.agents` is removed (a symlink or file unlinked, a directory removed without following symlinks) and each file written to `<workspace>/.agents/skills/<name>/<relative path>` with folders `0o700` and files `0o600` through `_replace_bytes`. The executable bit is not kept: a skill's scripts are run through their interpreter.
  - Executor preflight, in this order, each failure returned through `refused('Persistent session preflight failed: ...')` before dispatch: unset variables (message names them); for skills, `native.check_skill_homes(native.default_codex_home())`; the existing workspace check, with `.agents` left out of the names that refuse when skills are declared (`.codex` still refuses); then `transport.write_skills(workspace, skills, self.calls_root.parent / 'objects')`.

- [ ] **Step 1: Write the failing tests**

```python
def test_skills_are_written_fresh_into_the_workspace(self):                 # transport
    # objects/<sha> hold SKILL_MD and CHECK_PY; the workspace holds .agents/stale.md and .agents/skills/old/SKILL.md
    transport.write_skills(self.workspace, {'plot-check': PLOT_CHECK}, objects)
    found = sorted(str(p.relative_to(self.workspace / '.agents')) for p in (self.workspace / '.agents').rglob('*') if p.is_file())
    self.assertEqual(found, ['skills/plot-check/SKILL.md', 'skills/plot-check/scripts/check.py'])
    self.assertEqual((self.workspace / '.agents/skills/plot-check/SKILL.md').read_bytes(), SKILL_MD)
    self.assertEqual(stat.S_IMODE((self.workspace / '.agents/skills/plot-check/SKILL.md').stat().st_mode), 0o600)

def test_a_missing_changed_or_linked_blob_writes_nothing(self):
    # each case: the blob absent; the blob holding other bytes; the blob a symlink -> ValueError, stale .agents intact

def test_a_symlinked_agents_folder_is_replaced_not_followed(self):
    # .agents -> outside/ holding keep.txt: after write_skills, outside/keep.txt is still there and .agents is a real folder

def test_a_call_with_skills_writes_them_and_replaces_agents(self):         # executor
    # settings WITH_SKILLS, both blobs put with store.put_blob before dispatch, HOME and CODEX_HOME empty;
    # workspace_files makes .agents/notes.md: the call is dispatched and the workspace holds exactly the two files

def test_the_refusals_the_design_keeps(self):
    # WITH_SKILLS with a workspace .codex folder -> never_dispatched, error names .codex
    # TERMINAL with a workspace .agents folder -> never_dispatched as today

def test_an_unset_variable_refuses_the_call_before_dispatch(self):          # Review Focus 4
    # settings {**SEARCH, 'tool_servers': {'chem': here(CHEM)}}, CHEM_API_KEY unset: receipt status failed_definite,
    # never_dispatched True, 'CHEM_API_KEY' in error, transport.invoke not called

def test_a_codex_home_with_skills_refuses_a_skills_call(self):
    # CODEX_HOME patched to a folder holding skills/x: never_dispatched, 'separate Codex home' in error
```

- [ ] **Step 2: Run them and see them fail**

Run: `python3 -m unittest tests.v2.test_native_transport tests.v2.test_native_executor`

- [ ] **Step 3: Implement** `write_skills` and the preflight order above.

- [ ] **Step 4: Document** that the skills are written afresh into `.agents/skills` before every call, that commands cannot write there, that a workspace `.agents` is replaced only for a role with skills, and the unset-variable refusal.

- [ ] **Step 5: Run and commit**

Run: `python3 -m unittest discover -s tests`

```bash
git add rsi_lab/transport.py rsi_lab/roles.py tests/v2 docs/persistent-agents.md
git commit -m "Write a run's skills into the workspace before each call"
```

---

### Task 6: The request's short form and the prompt sentences

**Files:**
- Modify: `rsi_lab/native.py` (new `request_form`, `validate_request`; `instruction`), `rsi_lab/runtime.py` (`_context` line 333–335; `prepare_meta` lines 583 and 652), `rsi_lab/roles.py` (`MetaAgent.arguments` line 326; `CodexExecutor.__call__` line 834)
- Modify (docs): `docs/persistent-agents.md`: `### 请求的变化`
- Test: `tests/v2/test_native_requests.py`, `tests/v2/test_native_executor.py`

**Interfaces:**
- Consumes: Tasks 1 and 2's normal form.
- Produces:
  - `native.request_form(settings) -> dict`: each tool server becomes `{'description': ...}`; `skills` becomes the sorted list of names; everything else as in normal form, so settings without the two keys come back equal.
  - `native.validate_request(value) -> dict`: a checked copy of a request form (`None` is everything off); `view_image` and `skills` still need the terminal.
  - `native.instruction(request_form) -> str`: takes the request form. The tools list gains `viewing image files` after the terminal entry and `tool servers` last. After the terminal's sentence, with image viewing: `You can view image files in your working directory and in the directories the terminal can read; a picture you view is an unverified lead, not evidence. ` After the roles sentence, with tool servers: `Tool servers for this session, called as tools named mcp__<server>__<tool>: chem (Fingerprints and similarity for SMILES.); papers (Full text of open-access papers.). What a tool server returns is an unverified lead, not evidence. ` Nothing names skills: Codex lists them.
  - Runtime: the request's `task.constraints.native_capabilities` is `native.request_form(full)`; the Effect's `native_capabilities` is `full`, where `full = native.validate(self.native[role])` and `role` is computed as in `_context`; `read_native_snapshot` gets `full`.
  - Executor: refuses a packet whose `constraints.native_capabilities` differs from `request_form(settings)` (or is present when nothing is on), with `Unsupported exact model request envelope`.

- [ ] **Step 1: Write the failing tests**

```python
def test_the_request_carries_the_short_form(self):
    self.assertEqual(native.request_form(WITH_TOOL_SERVERS), {**SEARCH, 'tool_servers': {
        'chem': {'description': CHEM['description']}, 'papers': {'description': PAPERS['description']}}})
    self.assertEqual(native.request_form(WITH_SKILLS), {**TERMINAL, 'skills': ['plot-check']})
    for settings in (SEARCH, TERMINAL, PLAIN, WITH_ROLES, VIEW_IMAGE):
        self.assertEqual(native.request_form(settings), settings)
        self.assertEqual(native.validate_request(settings), settings)
    with self.assertRaises(ValueError):
        native.validate_request(WITH_TOOL_SERVERS)                          # the full form is not a request form

def test_the_two_prompt_additions(self):
    text = native.instruction(native.request_form({**VIEW_IMAGE, 'tool_servers': TOOL_SERVERS}))
    self.assertTrue(text.startswith('Native tools are enabled for this session: a terminal with file editing in your '
                                    'working directory, viewing image files, tool servers. Use them'))
    self.assertTrue(text.endswith(
        'record what matters in working_memory_delta. You can view image files in your working directory and in the '
        'directories the terminal can read; a picture you view is an unverified lead, not evidence. Tool servers for '
        'this session, called as tools named mcp__<server>__<tool>: chem (Fingerprints and similarity for SMILES.); '
        'papers (Full text of open-access papers.). What a tool server returns is an unverified lead, not evidence. '))
    self.assertTrue(native.instruction(native.request_form(URL_SERVER_ONLY)).startswith(
        'Native tools are enabled for this session: tool servers. Use them'))
    self.assertNotIn('plot-check', native.instruction(native.request_form(WITH_SKILLS)))

def test_full_settings_go_to_the_effect_and_the_short_form_to_the_request(self):
    # HOME and CODEX_HOME empty; config lab {'terminal': True, 'view_image': True,
    #   'tool_servers': {'chem': here(CHEM)}, 'skills': WITH_SKILLS['skills']}
    # effect['metadata']['native_capabilities'] == full; task_of(request)['constraints']['native_capabilities']
    #   == native.request_form(full); the prompt contains native.instruction(native.request_form(full));
    #   the packet bytes contain none of 'CHEM_MODE', sys.executable, PLOT_CHECK['files']['SKILL.md']

def test_requests_of_runs_without_the_new_keys_are_unchanged(self):           # Review Focus 3
    # the existing BASE_PLAIN_* test passes unedited; for SEARCH, TERMINAL and WITH_ROLES the Effect's settings
    # equal the request's

def test_the_executor_compares_the_request_form(self):                       # test_native_executor.py
    # packet_settings carrying the full WITH_TOOL_SERVERS form (here(CHEM)) under an Effect with the same full
    # settings -> ValueError 'Unsupported exact model request envelope'
```

- [ ] **Step 2: Run them and see them fail**

Run: `python3 -m unittest tests.v2.test_native_requests tests.v2.test_native_executor`

- [ ] **Step 3: Implement** as listed. `MetaAgent.arguments` reads the request copy with `validate_request`; the executor validates the Effect's copy with `validate`.

- [ ] **Step 4: Document** the two sentences and that the request carries tool servers by name and description and skills by name, while commands, arguments, `env` values and file hashes stay in the Effect.

- [ ] **Step 5: Run and commit**

Run: `python3 -m unittest discover -s tests`

```bash
git add rsi_lab/native.py rsi_lab/runtime.py rsi_lab/roles.py tests/v2 docs/persistent-agents.md
git commit -m "Send the short form of the new settings and tell the role about its servers and images"
```

---

### Task 7: The stream audit and the rollout reader

**Files:**
- Modify: `rsi_lab/native.py` (`stream_items`, `rollout_kinds`), `rsi_lab/transport.py` (`_events`, `audited_events`), `rsi_lab/session_context.py` (`_text_output`, `snapshot_from_bytes`), `tests/v2/native_fixtures.py`
- Modify (docs): `docs/persistent-agents.md`: `### 记录了什么` (the reader paragraphs)
- Test: `tests/v2/test_native_transport.py`, `tests/v2/test_native_session.py`

**Interfaces:**
- Consumes: Task 1's `tool_servers`.
- Produces:
  - `native.stream_items(settings)` adds `mcp_tool_call` when tool servers are declared; `native.rollout_kinds(settings)` allows `custom_tool_call` and its output for tool servers too.
  - `transport._events(stdout, allowed_items=frozenset(), tool_servers=frozenset())`: an `mcp_tool_call` item (any `item.*` event) whose `server` is not in `tool_servers` adds the error `Undeclared tool server: '<server>'`. `audited_events` passes the recorded settings' server names.
  - `session_context._text_output(output, images=False)`: with `images`, an item `{'type': 'input_image', 'image_url': 'data:image/<subtype>;base64,…'}` (optionally `detail`) is accepted without decoding; `<subtype>` matches `[A-Za-z0-9.+-]+`.
  - With `view_image` on, the snapshot gains `viewed_image_count` and `viewed_image_encoded_bytes` (characters after the comma of each accepted URL). Viewed images never enter `image_inputs` or `observable_image_bytes`; their bytes stay in `observable_text_history_bytes`. Snapshots of settings without `view_image` gain no key.
  - Fixtures (record shapes of the design, invented ids): `MCP_ITEM`; `MCP_RESULT = {'content': [{'type': 'text', 'text': 'similarity 0.42'}], 'isError': False}`; `mcp_call(call_id, server, tool, arguments, *, turn, status='completed', result=MCP_RESULT, duration=(0, 439666))` writing the `event_msg` `item_completed` with an `McpToolCall` item; `image_view(call_id, path, *, turn)` writing the `ImageView` event with `file://` + path; `viewed_image(call_id, path, data_url, *, turn)` writing the `custom_tool_call` of `const r = await tools.view_image({path:"…"}); image(r.image_url);`, the `ImageView` event and the `custom_tool_call_output` `[{'type': 'input_text', 'text': 'Script completed\n'}, {'type': 'input_image', 'image_url': data_url, 'detail': 'original'}]`.

- [ ] **Step 1: Write the failing tests**

```python
MCP_ITEM = {'type': 'mcp_tool_call', 'server': 'chem', 'tool': 'lookup', 'arguments': {'query': 'CCO'},
            'result': {'content': [{'type': 'text', 'text': 'similarity 0.42'}], 'structured_content': None},
            'error': None, 'status': 'completed'}
JPEG = 'data:image/jpeg;base64,' + base64.b64encode(b'\xff\xd8\xff' + bytes(3000)).decode()

def test_tool_server_items_only_for_a_declared_server(self):
    stream = [*NATIVE_STREAM[:2], {'type': 'item.completed', 'item': MCP_ITEM}, *NATIVE_STREAM[-2:]]
    allowed = native.stream_items(WITH_TOOL_SERVERS)
    self.assertEqual(transport._events(self.wire(stream), allowed, frozenset({'chem', 'papers'}))[3], [])
    other = [*stream[:2], {'type': 'item.started', 'item': {**MCP_ITEM, 'server': 'other'}}, *stream[2:]]
    self.assertIn("Undeclared tool server: 'other'", transport._events(self.wire(other), allowed, frozenset({'chem'}))[3])
    self.assertTrue(any('Forbidden tool' in e for e in transport._events(self.wire(stream), native.stream_items(SEARCH))[3]))

def test_audited_events_reads_the_declared_servers(self):
    # a folder whose invocation.json records WITH_TOOL_SERVERS and whose stream names 'chem' audits clean

def test_a_viewed_image_is_read_only_with_image_viewing(self):              # Review Focus 5 (test_native_session.py)
    raw = fixture(self.cwd) + viewed_image('call-v1', '/w/plot.jpg', JPEG, turn='turn-1')
    snapshot = snapshot_from_bytes(raw, expected_thread_id=THREAD, expected_cwd=self.cwd, native=VIEW_IMAGE)
    self.assertEqual((snapshot['viewed_image_count'], snapshot['viewed_image_encoded_bytes']),
                     (1, len(JPEG) - len('data:image/jpeg;base64,')))
    self.assertEqual((snapshot['image_inputs'], snapshot['observable_image_bytes']), ([], 0))
    self.assertEqual(snapshot['observable_text_history_bytes'], len(raw))
    for settings in (TERMINAL, SEARCH, LOCAL_SERVER_ONLY):
        with self.subTest(settings=settings), self.assertRaises(ContextUnavailable):
            snapshot_from_bytes(raw, expected_thread_id=THREAD, expected_cwd=self.cwd, native=settings)
    for url in ('file:///w/plot.png', 'https://images.example.org/plot.png', 'data:text/plain;base64,eA=='):
        with self.subTest(url=url), self.assertRaises(ContextUnavailable):
            snapshot_from_bytes(fixture(self.cwd) + viewed_image('c', '/w/p', url, turn='turn-1'),
                                expected_thread_id=THREAD, expected_cwd=self.cwd, native=VIEW_IMAGE)

def test_viewed_images_count_against_the_history_limit(self):
    # with the snapshot above and a packet P: build_effective_context(P, ..., snapshot=snapshot,
    # max_input_bytes=len(raw) + len(P) - 1) is 'blocked_capacity' with total_image_bytes 0; one byte more is 'ready'

def test_tool_server_records_are_read_under_a_server_alone(self):
    # fixture + tool_call('c1', 'text(await tools.mcp__chem__lookup({query:"CCO"}));') + mcp_call(...) is read under
    # LOCAL_SERVER_ONLY and refused with everything off; snapshots under TERMINAL gain no viewed_image_* key
```

- [ ] **Step 2: Run them and see them fail**

Run: `python3 -m unittest tests.v2.test_native_transport tests.v2.test_native_session`

- [ ] **Step 3: Implement** as listed; `snapshot_from_bytes` passes `images=settings.get('view_image', False)`.

- [ ] **Step 4: Document** that a role with tool servers may have `mcp_tool_call` stream items for declared servers only, and how viewed images are read and counted.

- [ ] **Step 5: Run and commit**

Run: `python3 -m unittest discover -s tests`

```bash
git add rsi_lab/native.py rsi_lab/transport.py rsi_lab/session_context.py tests/v2 docs/persistent-agents.md
git commit -m "Read tool server items and viewed images in the stream and the rollout"
```

---

### Task 8: Recording tool server calls and viewed images

**Files:**
- Modify: `rsi_lab/codex_ledger.py` (`read_turn`, `read_child`, `settle_call`)
- Modify (docs): `docs/persistent-agents.md`: `### 记录了什么`
- Test: `tests/v2/test_codex_ledger.py`, `tests/v2/test_native_executor.py`

**Interfaces:**
- Consumes: Task 7's fixtures; Task 1's `tool_servers`.
- Produces:
  - `codex_ledger.MAX_TOOL_SERVER_CALLS = 32`, `codex_ledger.MAX_IMAGE_PATHS = 32`.
  - `read_turn` and `read_child` return `tool_server_calls` (the first 32 `McpToolCall` items), `tool_server_calls_omitted` and `images_viewed`. An entry is `{'server', 'tool', 'status', 'call_id', 'arguments_sha256', 'arguments_excerpt', 'result_bytes', 'duration_seconds'}`: `call_id` is the item's `id`; the argument text is the string itself or `json.dumps(arguments, ensure_ascii=False, sort_keys=True)`, hashed whole and cut to `EXCERPT_CHARS`; `result_bytes` is the UTF-8 length of `store.encode(result)`, `None` for a `null` result; `duration_seconds` is `secs + nanos / 1e9`, `None` when malformed; a field of the wrong type is `None`. `images_viewed` is `{'paths': [first 32 ImageView paths], 'paths_omitted': int, 'count': number of input_image items in tool outputs, 'bytes': their decoded sizes, 3 * len(payload) // 4 - payload[-2:].count('='), summed}`.
  - What counts: in the lead, the records after the prefix; in a sub-agent, events whose `turn_id` is one of its counted turns, and for tool outputs (which carry no turn id) the records that lie after a `turn_context` of a counted turn and before the next `turn_context`.
  - `settle_call`: `native_activity` and each `child_threads` entry gain `tool_server_calls` and `tool_server_calls_omitted` only when the settings declare tool servers, and `images_viewed` only with `view_image`. Other receipts keep their keys.

- [ ] **Step 1: Write the failing tests**

```python
ARGS = json.dumps({'query': 'CCO'}, ensure_ascii=False, sort_keys=True)

def test_the_lead_records_its_tool_server_calls(self):
    raw = (fixture(self.cwd) + mcp_call('exec-1111', 'chem', 'lookup', {'query': 'CCO'}, turn='t1')
           + mcp_call('exec-1112', 'papers', 'fetch', {'doi': '10.0000/invented'}, turn='t1', status='failed', result=None)
           + usage_row(THREAD, 't1', 't1', 10, 1))
    turn = codex_ledger.read_turn(self.write(raw), thread_id=THREAD, prefix_bytes=0)
    self.assertEqual(turn['tool_server_calls'][0], {
        'server': 'chem', 'tool': 'lookup', 'status': 'completed', 'call_id': 'exec-1111',
        'arguments_sha256': hashlib.sha256(ARGS.encode()).hexdigest(), 'arguments_excerpt': ARGS,
        'result_bytes': len(encode(MCP_RESULT)), 'duration_seconds': 0.000439666})
    self.assertEqual((turn['tool_server_calls'][1]['result_bytes'], turn['tool_server_calls_omitted']), (None, 0))

def test_more_calls_than_the_limit_are_counted_not_kept(self):
    # 35 calls, one with 300-character arguments: 32 kept, 3 omitted, every excerpt at most 200 characters

def test_viewed_images_are_listed_with_their_count_and_bytes(self):
    # viewed_image of a PNG data URL of 68 bytes at /w/plot.png and of JPEG at /w/fit.jpg:
    # images_viewed == {'paths': ['file:///w/plot.png', 'file:///w/fit.jpg'], 'paths_omitted': 0, 'count': 2,
    #                   'bytes': 68 + 3003}

def test_a_child_records_only_this_calls_turns(self):
    # child rollout: own session_meta; turn_context c0 + an mcp_call + a viewed image under c0 (an earlier call);
    # turn_context c1 + one mcp_call + one viewed image under c1; usage rows (c0, t0) and (c1, t1)
    child = codex_ledger.read_child(self.home, CHILD, parent_thread_id=THREAD, root_turn_ids=['t1'])
    self.assertEqual(([c['call_id'] for c in child['tool_server_calls']], child['images_viewed']['count']), (['exec-c1'], 1))

def test_receipts_carry_the_records_only_for_roles_that_have_the_tool(self):   # test_native_executor.py
    # {**SEARCH, 'tool_servers': {'chem': here(CHEM)}} with CHEM_API_KEY set: native_activity has tool_server_calls,
    #   no images_viewed; VIEW_IMAGE: images_viewed only; TERMINAL_AND_SUBAGENTS: neither, in native_activity or in
    #   any child_threads entry
```

- [ ] **Step 2: Run them and see them fail**

Run: `python3 -m unittest tests.v2.test_codex_ledger tests.v2.test_native_executor`

- [ ] **Step 3: Implement** as listed; one helper finds a sub-agent's records of the counted turns by position.

- [ ] **Step 4: Document** both records, for the lead and in `child_threads`, and that the call id is the `_meta.callId` the server receives, so a server's own log can be matched to the record.

- [ ] **Step 5: Run and commit**

Run: `python3 -m unittest discover -s tests`

```bash
git add rsi_lab/codex_ledger.py tests/v2 docs/persistent-agents.md
git commit -m "Record each call's tool server calls and viewed images"
```

---

### Task 9: Reading the skills lists and the check after each call

**Files:**
- Modify: `rsi_lab/codex_ledger.py`, `rsi_lab/roles.py` (after `settle_call`), `rsi_lab/call_recovery.py` (after `settle_call`), `tests/v2/native_fixtures.py`
- Modify (docs): `docs/persistent-agents.md`: `### 技能` (the check and `skills_shown`)
- Test: `tests/v2/test_codex_ledger.py`, `tests/v2/test_native_executor.py`, `tests/v2/test_native_recovery.py`

**Interfaces:**
- Consumes: Task 2's `declared_skills`; Task 8's `settle_call` receipt shape.
- Produces:
  - `codex_ledger.SKILLS_LIST_START = '<skills_instructions>'`. A skills list is a `response_item` `message` with `role` `developer` whose first `input_text` begins with it.
  - `codex_ledger.parse_skills_list(text) -> {'roots': {label: path}, 'skills': [{'name', 'description', 'root', 'file'}]}`, raising `ValueError`. The text ends with `</skills_instructions>`. `### Skill roots` and `### Available skills` start sections; any other line beginning with `#` ends one. In the roots section every line beginning with `- ` must match ``- `(r\d+)` = `(.+)` ``; in the skills section every such line must match `- ([^:]+): (.*) \(file: (r\d+)/(.+)\)` (greedy description, so the last `(file: …)` wins) with a known label. Both sections are present and the roots section is not empty. Other lines are ignored.
  - `codex_ledger.compare_skills(lists, *, declared, workspace) -> {'shown': [str], 'unexpected': int, 'folders': [str], 'missing': [str], 'unreadable': int}`: a skill is shown when its root resolves to `<workspace>/.agents/skills` resolved, its file is `<name>/SKILL.md` and its name is declared and not yet seen in that list; any other skill is unexpected, its root path as written going to `folders`. `shown` comes from the last readable list; `missing` collects declared names absent from any list.
  - `read_turn` returns `skills_lists` (texts after the prefix) and `earlier_skills_lists` (texts in the prefix); `read_child` returns `skills_lists` (texts in its whole rollout).
  - `settle_call(..., workspace=None)` returns `skills_error: str | None`, never a skill name nobody declared:
    - role without skills (everything off included): the lead's new lists and those of each sub-agent started in this call must be none; a sub-agent's list whose text equals one of `earlier_skills_lists` is a copy of the lead's history and ignored. Error: `Codex showed a skills list to a role without skills: <thread>: <n> skills from <folders>`.
    - role with skills: every such list is compared, copies included; an unexpected or missing skill, an unreadable list, an unreadable rollout of the lead or of a started sub-agent, or no list in a finished call that started the lead's thread or a sub-agent is an error: `Codex showed skills other than the declared ones: <thread>: <n> unexpected from <folders>; missing <names>; <k> lists unreadable; no list`, parts present only when they apply.
  - Receipt, role with skills only: `native_activity['skills_shown']` in a call that started the lead's thread, each started `child_threads` entry's `skills_shown`, and `skills_unexpected: {'count', 'folders'}` beside them when the count is above 0. `skills_lists` and `earlier_skills_lists` are never in a receipt.
  - Executor and recovery treat `skills_error` as they treat `profile_error`: `protocol_valid` false, the message joined into `error`. Both pass `workspace`.
  - Fixtures `skills_list_text(roots, *skills) -> str` (the list text of the design's record shape, with a line of prose under `## Skills`) and `skills_list(roots, *skills) -> bytes` (the `developer` message record holding it); `skills` are `(name, description, file)` tuples with `file` such as `r0/plot-check/SKILL.md`.

- [ ] **Step 1: Write the failing tests**

```python
def ws_root(self): return str(self.cwd / '.agents' / 'skills')
DECLARED = ('plot-check', 'Checks a plot before it is reported.', 'r0/plot-check/SKILL.md')

def test_a_list_is_parsed(self):
    text = skills_list_text([self.ws_root()], DECLARED, ('odd', 'Shows (file: x) inside.', 'r0/odd/SKILL.md'))
    self.assertEqual(codex_ledger.parse_skills_list(text)['skills'][1],
                     {'name': 'odd', 'description': 'Shows (file: x) inside.', 'root': 'r0', 'file': 'odd/SKILL.md'})
    for broken in (text.replace('### Skill roots', '### Roots'), text.replace('r0/odd', 'r1/odd'),
                   text.replace('- odd: ', '- odd '), text.replace('</skills_instructions>', '')):
        with self.subTest(broken=broken), self.assertRaises(ValueError):
            codex_ledger.parse_skills_list(broken)

def test_a_list_as_declared_is_recorded(self):
    got = codex_ledger.compare_skills([skills_list_text([self.ws_root()], DECLARED)], declared=['plot-check'],
                                      workspace=str(self.cwd))
    self.assertEqual(got, {'shown': ['plot-check'], 'unexpected': 0, 'folders': [], 'missing': [], 'unreadable': 0})

def test_an_extra_skill_is_counted_with_its_folder_never_named(self):
    # roots [ws_root, '/u/.agents/skills'], skills DECLARED and ('secret-notes', 'x', 'r1/secret-notes/SKILL.md'):
    # settle_call under WITH_SKILLS -> skills_error contains '1 unexpected from /u/.agents/skills';
    # 'secret-notes' appears in neither the error nor json.dumps(receipt);
    # receipt['native_activity']['skills_unexpected'] == {'count': 1, 'folders': ['/u/.agents/skills']}

def test_a_missing_or_unreadable_list_or_none_at_all_invalidates(self):
    # under WITH_SKILLS on a fresh thread: a list without plot-check -> 'missing plot-check'; a broken list ->
    # '1 lists unreadable'; no list in a finished call -> 'no list'; no list in an unfinished call -> no error

def test_a_list_for_a_role_without_skills_invalidates(self):
    # TERMINAL and everything off (OFF settings), the lead's new part holding a list of two skills ->
    # skills_error starts 'Codex showed a skills list to a role without skills' and contains '2 skills from'

def test_a_resumed_thread_that_got_a_second_list(self):
    # WITH_SKILLS, prefix holding the declared list: the call's part with the declared list again -> no error;
    # with a list adding an undeclared skill -> error; with no list -> no error (resumed); no skills_shown either way

def test_a_sub_agent_thread_is_checked(self):
    # WITH_SKILLS + sub-agents: a child started in the call whose rollout holds the declared list -> its entry has
    # skills_shown ['plot-check']; a started child without a list -> 'no list'; a child only addressed again, without a
    # list -> no error and no skills_shown

def test_a_forked_copy_of_an_old_lead_list_is_not_the_sub_agents_own(self):     # Review Focus 1
    # TERMINAL_AND_SUBAGENTS (no skills); the lead's prefix holds OLD = a list of 'personal-notes' under
    # '/u/.codex/skills'; a child started in the call, forked, whose rollout holds OLD -> no error;
    # the same child holding another list instead -> error

def test_root_spellings_and_extra_lines(self):                                   # Review Focus 2
    # the root written through a symlink to the workspace, and with a trailing slash -> shown; a prose line
    # 'Use them when relevant.' inside "Available skills" is ignored; a root of <workspace>/.codex/skills -> unexpected

def test_the_executor_and_recovery_make_the_call_invalid(self):
    # test_native_executor.py and test_native_recovery.py: an extra skill in the call's list -> receipt protocol_valid
    # False and 'unexpected' in error, tokens still charged
```

- [ ] **Step 2: Run them and see them fail**

Run: `python3 -m unittest tests.v2.test_codex_ledger tests.v2.test_native_executor tests.v2.test_native_recovery`

- [ ] **Step 3: Implement** as listed.

- [ ] **Step 4: Document** the check after each call, `skills_shown` and `skills_unexpected`, that an undeclared skill is recorded by count and folder only, and that a thread started before this change keeps the list Codex gave it then.

- [ ] **Step 5: Run and commit**

Run: `python3 -m unittest discover -s tests`

```bash
git add rsi_lab/codex_ledger.py rsi_lab/roles.py rsi_lab/call_recovery.py tests/v2 docs/persistent-agents.md
git commit -m "Check after each call that Codex showed exactly the declared skills"
```

---

### Task 10: Stopping a local tool server's processes

**Files:**
- Modify: `rsi_lab/transport.py` (`ARTIFACTS`, `_stream_process`, `_terminate`, `_sweep`, `invoke`; new `write_tracked`, `read_tracked`, `stop_recorded`), `rsi_lab/call_recovery.py` (`recover_codex_receipt` and its docstring)
- Modify (docs): `docs/persistent-agents.md`: `### 进程`, `### 恢复与隔离`; `README.md`: one sentence beside the native-capabilities paragraph naming tool servers, image viewing and skills
- Test: `tests/v2/test_native_transport.py`, `tests/v2/test_native_recovery.py`

**Interfaces:**
- Consumes: Task 1's `local_servers`.
- Produces:
  - `transport.TRACKED_FILE = 'tracked_processes.json'`, added to `ARTIFACTS`, holding `{"processes": [{"pid": int, "start": str}, …]}` in pid order.
  - `transport.write_tracked(folder, tracked) -> None` (through `_replace_bytes`); `transport.read_tracked(folder) -> dict[int, str]` (`{}` when the file is missing, `ValueError` when malformed); `transport.stop_recorded(folder) -> bool` (`_stop` of what the file names; `False` when the file is malformed).
  - `_stream_process(..., tracked=None, record=None)`, `_terminate(process, tracked=None, record=None)`, `_sweep(tracked, record=None)`: `record(tracked)` is called whenever a sample adds a process, before any signal. `invoke` passes `lambda tracked: write_tracked(folder, tracked)`.
  - `invoke` tracks when `settings['terminal'] is not None or native.local_servers(settings)`; `descendants_stopped` keeps its meaning.
  - `recover_codex_receipt`: right after the journal and session binding checks, before it looks for terminals, calls `stop_recorded(folder)` when the recorded settings track; `native_writer_quiesced` additionally needs that to return `True`. It still launches no Codex and makes no provider request; it reads the process table and signals recorded processes after the pid and start-time check.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_local_server_alone_tracks_and_stops_what_the_call_orphaned(self):
    marker = self.root / 'server.pid'; self.addCleanup(self.stop_recorded, marker)
    with patch.object(transport, 'TRACK_SECONDS', .1), patch.object(transport, 'codex_version', return_value='0.170.0'), \
            patch.dict(os.environ, {'CHEM_API_KEY': 'invented'}):
        result = self.invoke(code=self.orphaning_codex(marker, ends=True), native=LOCAL_SERVER_ONLY)
    self.assertEqual(result['execution_status'], 'succeeded', result['error'])
    self.assertTrue(result['descendants_stopped'])
    pid = int(marker.read_text().split()[0])
    self.assertIn(pid, transport.read_tracked(self.folder))
    self.assert_stopped(marker)

def test_a_url_server_starts_no_tracking(self):
    # URL_SERVER_ONLY with PAPERS_TOKEN set: _process_table not called, no tracked_processes.json

def test_the_tracked_file_is_written_before_a_kill(self):
    # orphaning_codex(..., ends=False), timeout 2.5: tracked_processes.json names the grandchild with its start time

def test_recovery_stops_what_the_call_recorded(self):                         # test_native_recovery.py
    # a sleeping process started in its own session with transport._POPEN (setUp patches subprocess.Popen);
    # write_call(LOCAL_SERVER_ONLY, stream=TOOL_FREE_STREAM, turn=rows('t2'), saved=False) and a tracked file naming it
    # with its real start: recover -> the process is gone and native_writer_quiesced is False (no saved result);
    # the same with a recorded start that differs -> the process is not signalled (the test stops it);
    # a malformed tracked file with a saved result that says descendants_stopped True -> quiesced False;
    # a call with no terminal yet -> recover returns None and the recorded process is still stopped
```

- [ ] **Step 2: Run them and see them fail**

Run: `python3 -m unittest tests.v2.test_native_transport tests.v2.test_native_recovery`
Expected: the new tests fail; `test_a_call_without_the_terminal_never_reads_the_process_table` still passes.

- [ ] **Step 3: Implement** as listed.

- [ ] **Step 4: Document** that tracking runs for a local tool server with or without a terminal, the tracked file, that recovery stops recorded processes before it settles anything, and that recovery must not run while another process is still executing that call (the PABLO host's lock ensures this; the kernel CLI's `recover` does not). Add the README sentence.

- [ ] **Step 5: Run and commit**

Run: `python3 -m unittest discover -s tests`

```bash
git add rsi_lab/transport.py rsi_lab/call_recovery.py tests/v2 docs/persistent-agents.md README.md
git commit -m "Stop a local tool server's processes after its call and on recovery"
```

---

### Task 11: The PABLO host

**Files:**
- Modify: `experiments/pablo/pablo_task/team.py` (`native_block`, `not_blind`, `preparation`), `experiments/pablo/run.py` (`prepare`, between `store = Store(root)` and `Controller(store).initialize`), `experiments/pablo/TEAM_V1.md`
- Test: `experiments/pablo/tests/test_team.py` (runs here), `experiments/pablo/tests/test_host_team_preflight.py` (needs the scorer; written, **not run**)

**Interfaces:**
- Consumes: Task 1's settings and fixtures shape; Task 2's `freeze_skills`.
- Produces:
  - `team.TOOL_SERVERS_TEMPLATE = 'Tool servers are on for {}: the hidden objective is protected by instruction only, and this run is not a blind run.'`, roles named as `NOT_BLIND_TEMPLATE` names them.
  - `team.not_blind(block)`: the web-search sentence, the tool-server sentence (any role with a local or URL server), or both joined by one space in that order; `None` when neither. `preparation` passes it the block `native_block` returned, whose skills are already frozen.
  - `team.native_block(block, *, protected)`: unchanged checks, then `native.freeze_skills(result)` (no store) before the kernel's validation, and a refusal `native_capabilities.<role>.skills.root: <reason>` when `exposes_objective(root, protected)` gives one. Returns the dry-frozen block. `check_readable_roots` at each start is unchanged: the frozen copy cannot change.
  - `run.prepare` with `--team`: `config['native_capabilities'] = native.freeze_skills(config['native_capabilities'], store.put_blob)` before `initialize`, which re-reads each skills folder and refuses one that changed since `native_block`. A run with skills is started with `CODEX_HOME` set to the separate home; the host's `make_runtime` reaches Task 4's check through `Runtime`.

- [ ] **Step 1: Write the failing tests** in `test_team.py`. `self.skills` is a temporary folder made with `tests.v2.native_fixtures.skill_folder`; `self.checkout` is a temporary stand-in for the repository checkout.

```python
PAPERS = {'description': 'Full text of open-access papers.', 'url': 'https://papers.example.org/mcp'}

def test_tool_servers_make_the_run_not_blind(self):
    chem = {'description': 'Fingerprints.', 'command': sys.executable}
    self.assertEqual(team.not_blind({'lab': {'subagents': True, 'tool_servers': {'papers': PAPERS}},
                                     'meta': {'tool_servers': {'chem': chem}}}),
                     'Tool servers are on for the Lab and Meta: the hidden objective is protected by instruction '
                     'only, and this run is not a blind run.')
    both = {'lab': {'subagents': True, 'web_search': True, 'tool_servers': {'papers': PAPERS}}}
    self.assertEqual(team.not_blind(both), team.NOT_BLIND + ' ' + team.TOOL_SERVERS_TEMPLATE.format('the Lab'))

def test_the_new_keys_pass_through_the_kernel_validation(self):
    for bad in ({'lab': {'subagents': True, 'view_image': True}},
                {'lab': {'subagents': True, 'tool_servers': {'p': {**PAPERS, 'url': 'http://papers.example.org/mcp'}}}}):
        with self.subTest(bad=bad), self.assertRaises(ValueError):
            team.native_block(bad, protected=[])
    block = team.native_block({'lab': {'subagents': True, 'terminal': True, 'view_image': True,
                                       'tool_servers': {'papers': PAPERS}, 'skills': {'root': str(self.skills)}}},
                              protected=[self.checkout])
    self.assertEqual((block['lab']['view_image'], block['lab']['tool_servers']), (True, {'papers': PAPERS}))
    self.assertEqual(sorted(block['lab']['skills']['frozen']), ['plot-check'])

def test_a_skills_folder_that_exposes_the_scorer_is_refused(self):
    # a skill holding pablo_task/oracle.py, and a skills folder inside the checkout: each raises ValueError whose
    # text contains 'skills.root'
```

In `test_host_team_preflight.py` (not run): a `--team` preparation with a skills folder stores both files as blobs, freezes them in the config and records the tool-server sentence in `pablo_not_blind`; a folder changed between `native_block` and the store is refused.

- [ ] **Step 2: Run and see them fail**

Run: `PYTHONPATH=experiments/pablo:. "$PABLO_PY" -m unittest experiments.pablo.tests.test_team`

- [ ] **Step 3: Implement** as listed.

- [ ] **Step 4: Document** in `TEAM_V1.md`: the tool-server sentence beside the web-search one in `## 网络搜索使运行不再是盲测`, and the skills folder among the refused places in `## 被拒绝的终端可读目录`; that a run with skills is started with `CODEX_HOME` set to the separate home.

- [ ] **Step 5: Run everything that can run, and commit**

Run: `test_team`, `test_team_selection` and `test_harness` with `$PABLO_PY`; `python3 -m unittest discover -s tests`.
Expected: all pass. Report `test_host_team_preflight.py` as written and **not run**.

```bash
git add experiments/pablo
git commit -m "Let PABLO team runs declare tool servers, image viewing and skills"
```

---

## After the build (not a task)

The design's real check through the executor is owed and needs the owner's go-ahead: one call with a local server (a secret by `env_from`), a URL server, an image that is not a small PNG and a skill, on a lead and a role sub-agent, accepted by every kernel check with the record showing the calls, the image and exactly the declared skills; a killed turn leaving no local server behind; an image outside the allowed folders refused. It should also confirm where Codex writes a forked sub-agent's own skills list relative to the copied history, which Task 9 does not rely on.
