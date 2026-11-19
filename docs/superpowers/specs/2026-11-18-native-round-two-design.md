# Native capabilities, round two: tool servers, image viewing and skills

Date: 2026-11-18, rewritten 2026-11-19 after real calls on Codex CLI 0.170.0.
Status: approved by the owner on 2026-11-19.
Builds on `2026-11-17-native-codex-capabilities-design.md` (pull request 9),
`2026-11-18-lab-team-design.md` (pull request 10) and the adaptation to Codex
0.170.0 (pull request 11). This branch is cut from that last branch.

Lines marked **(my call)** are choices the owner did not make; they are open to
rejection in review.

## Intent

The first round opened Codex's web search, terminal and sub-agents to the
agents and kept everything else off. On 2026-11-18 the owner widened that:
"Codex 的桌面和浏览器控制、账户连接器和插件、图像生成、用户钩子、宿主技能，以及
外部 MCP 服务。这几个功能如果对科研有帮助，也是可以开放的。" Asked what to add
now, they said "都加，开查看图片": tool servers of any kind, image viewing and
skills, plus browser control if it can be done.

Reviewing the first draft, which had the kernel write its own list of skills
into the prompt, the owner said: "用 Codex 原生的技能加载方式，做好与我平时用的
codex的隔离". So Codex finds and lists a run's skills itself, and what the run
sees is kept apart from the Codex the owner uses every day.

On 2026-11-19, shown a list of what the draft left out, the owner said:
"工具服务、查看图片、skill、插件、按网址连接的工具服务、写技能内容、写子agent、
或让 Meta 写技能，这几个我都想Lab-codex有". They then agreed to an order: first
make the kernel work on the new Codex (done, pull request 11); then this
document, which covers loading: tool servers started locally or reached by URL,
image viewing and skills; then plugins; then writing, where the Lab writes
skills and sub-agent roles and Meta writes skills; then the literature service
that lets a sub-agent bring back citable sources, built on the tool server
mechanism described here.

## Goals

1. A run config can declare tool servers for a role, started as a local command
   or reached by URL. The role and its sub-agents call their tools inside a
   turn, and the kernel records every call and stops every server process it
   started when the call ends.
2. A role can be allowed to view image files, under the same folder limits as
   its terminal.
3. A run can carry a folder of skills, frozen with the run. Codex finds and
   lists them itself, for the agent and for each of its sub-agents, and an agent
   reads one when it needs it.
4. A model is shown exactly the skills declared for its run: never the owner's
   personal skills, Codex's bundled ones, or skills lying in other folders.
   Today every run shows the first two. Each call's record says what was shown.
5. Nothing here changes the Codex the owner uses every day: not its config, its
   skills or its credentials.
6. A run that switches none of this on keeps its stored settings, its request
   and its receipts as they are, apart from the change in goal 4.

## Non-goals

- Browser and desktop control. They do not exist when Codex runs from the
  command line (see "What was checked"). Reading a public web page is already
  possible through web search.
- Plugins. They are the next design. In a real call a plugin's skill and its
  read-only tool worked, but its other tool was refused for want of approval,
  and Codex downloaded about 96 MB of its own plugin catalogue into the Codex
  home during the call, which then took 335 seconds.
- Account connectors, image generation and user hooks. The owner did not ask
  for them.
- Making what a tool server returns citable. That is the second design. Here
  every tool server result is a lead.
- Tool servers that need a sign-in in a browser. A URL server is given a token
  from an environment variable or nothing. **(my call)**
- Writing skills or sub-agent roles during a run. This design delivers the
  folder and its loading; writing is a later design. What it must not block is
  noted in section 6.
- Running with skills inside a Codex home that has skills of its own. Such a
  run is refused; it is not made to work by hiding the owner's skills.
- Moving existing runs to another Codex home.
- Limiting a tool server to one sub-agent role. Codex could not do it on 0.163.0;
  not rechecked on 0.170.0.
- Hiding from a tool server what Codex tells it about the machine (section 2).

## Anti-goals

- A tool server process that outlives its call. The owner chose not to cap
  literature tool calls; a server left running after a killed turn could go on
  making outside requests with nothing recording them.
- Image viewing that can read pictures outside the folders the role is allowed.
- A model that sees skills nobody declared for the run.
- A run record that names the owner's personal skills.
- Credentials copied or linked from one Codex home to another.
- A secret written into the run config, a call record or a process's command
  line.
- A URL tool server declared without the run being recorded as not blind.

## Constraints

- Codex CLI 0.170.0, macOS; the kernel refuses any other version (pull request
  11). The facts under "What was checked" come from three spikes: eleven
  low-effort calls on 0.163.0 on 2026-11-18, ten on 0.170.0 on 2026-11-19 made
  through the kernel's own transport in a separate Codex home, and offline
  checks with Codex's prompt preview (`codex debug prompt-input`, which calls
  no model). Each row says which.
- The kernel stays standard-library only.
- The repository is public: sample data in tests and documents is invented.
- Nothing may open, list or scan `~/.codex/sessions` or
  `~/.codex/archived_sessions`.
- Whatever is opened is declared in the run config and frozen with the run,
  never taken from the owner's personal Codex setup. **(my call, stated to the
  owner)**

## Approaches

**Tool servers.** A server reached by URL is declared the same way as a local
one and passed to Codex, which connects to it; the alternative, a local
forwarding server, adds a process for nothing. For local servers three ways
were considered. Codex starting a local server per
call (chosen by the owner): no ports, no standing process, the server lives as
long as the call. The Controller running a local HTTP server: more state to
manage and recover, and other local programs could try to connect. No server at
all, with the sub-agent limited to web search: it cannot read full text or bring
back citable sources.

**Skills.** Codex loads skills itself (the owner's decision): it looks in a
fixed set of folders and lists what it finds to the model. In the owner's
everyday Codex home that list includes their personal skills, so the question
is how to keep those out. Three ways were considered.

- *A Codex home with no skills of its own* (chosen, **my call**). A run with
  skills runs in a separate Codex home that the owner logs in to once. Nothing
  personal lies where Codex looks, the command is the same on every call, and
  no record mentions the owner's skills. Cost: one login by the owner.
- *The everyday home, with each personal skill switched off per call.* Codex
  can switch off one skill by path (`skills.config`). No login is needed. But
  the kernel would read the owner's skills folder before every call; the paths
  of their skills would be stored with every call, because the kernel keeps
  each call's command line; and the command would change whenever the owner
  installs or removes a skill.
- *Codex's list switched off and a list written by the kernel.* The first
  draft. Rejected by the owner.

**Image viewing.** It is allowed only together with the terminal (approved by
the owner). Without the terminal a call runs in Codex's read-only sandbox, which
can read any file on the machine, so the image tool could show any picture on
it. The alternative, a folder-limited profile for image viewing alone, adds a
second kind of confined call for little gain.

## What was checked

| Fact | How |
|---|---|
| A server given only as `-c mcp_servers.<name>.command/args/env` is started under the kernel's flags, including `--ignore-user-config`, and its tools are called | real calls, 0.163.0 and 0.170.0 |
| A server given as `-c mcp_servers.<name>.url` with `bearer_token_env_var` is connected to and its tool is called; the token arrives as an `Authorization: Bearer` header. `http_headers` adds fixed headers | real call, 0.170.0 (headers: preview) |
| `mcp_servers.<name>.env_vars=["NAME"]` hands a local server that variable from the environment Codex was started in | real call, 0.163.0 only |
| Tools marked read-only run without approval; others are refused with "MCP tool call requires approval, but approval policy is never" unless the server has `default_tools_approval_mode="approve"`, with which they run | real calls, both versions |
| A sub-agent can call a session's tool servers. Codex starts one process per thread for a local server: the lead and each sub-agent get their own | real calls, both versions |
| A local server receives the environment given to it plus a few basics (`HOME`, `PATH`, `USER`, `TMPDIR` and the like) | real calls, both versions |
| At connection a server learns only the client's name and version. Every tool call carries `_meta` with a call id, thread and session ids and `x-codex-turn-metadata`, which holds the model, effort, sandbox mode, Codex version and `workspaces`: when the workspace lies inside a git repository, that repository's local path, the URL of its remote, its latest commit and whether it has changes. A URL server receives the same body | real call, 0.170.0 |
| The calling thread's stream has an `mcp_tool_call` item (`server`, `tool`, `arguments`, `result`, `status`); its rollout has an `event_msg` / `item_completed` item of type `McpToolCall`, beside the `custom_tool_call` record of the script that made the call. The kernel's stream audit refuses the item today | real call, 0.170.0 |
| A 60,000-character result reaches the model whole; a 120-second tool call completes | real calls, 0.163.0 only |
| At a normal end Codex sends a local server SIGTERM and it exits | real calls, both versions |
| A local server is the leader of its own process group, so killing Codex's group leaves it running as an orphan until its current call returns | real call, 0.163.0 only |
| `view_image` shows an image in the workspace | real calls, both versions |
| `view_image` refuses, with "Operation not permitted", an image in the denied run root and one outside the readable roots | real call, 0.163.0 only |
| A viewed image is written into the rollout as an `input_image` item with a `data:` URL inside the `custom_tool_call_output`, beside an `event_msg` item of type `ImageView` with the file path. The stream has no item for it. The kernel's rollout reader refuses that output today | real call, 0.170.0 |
| Under the kernel's flags a role is shown the skills of the Codex home it runs in and Codex's bundled ones; in a home without skills, the four bundled ones. Starting Codex there installs them under `skills/.system` | real calls, both versions |
| `-c skills.include_instructions=false` removes the whole skills list | real calls, both versions |
| Where Codex looks for skills under the kernel's flags: `.agents/skills` and `.codex/skills` in the working directory and in every folder above it up to the root of the enclosing repository; `skills` in the Codex home, with the bundled ones in `skills/.system`; and `$HOME/.agents/skills`, whatever the Codex home is | preview, both versions |
| `-c project_root_markers=[]` stops the search above the working directory, and `-c skills.bundled.enabled=false` drops the bundled skills from the list | preview, both versions |
| In a separate Codex home, with those two settings, the list of a real call holds exactly the skills of the workspace's `.agents/skills`, and the model opens a skill's file with the terminal | real call, 0.170.0 |
| The list is the text of one `developer` message in a thread's rollout. Every sub-agent thread has its own, built when it starts, under the same settings as its lead | real calls, 0.170.0 |
| A thread resumed with the same skills gets no second list. Resumed after the skills in the folder changed, it gets a new message with the whole list | real calls, 0.170.0, one case each |
| A skill written into the folder during a call is in the list of a sub-agent started afterwards in that call | real call, 0.170.0 |
| With the terminal's profile, `.agents/skills` can be made writable while the rest of `.agents` stays read-only | real call, 0.170.0 |
| A `skills.config` entry switches one skill off, chosen by `name` or by the path of its `SKILL.md`. It cannot add a folder to look in | preview, 0.163.0 |
| A skill whose `agents/openai.yaml` names tool servers it depends on does not make Codex start them when the session starts | preview, 0.170.0 |
| With the terminal off the model cannot open a skill file | real call, 0.163.0 only |
| The kernel stores each call's command line in the call's folder, and one recovery path rebuilds the command and compares | the code |
| A command-line call has no browser or desktop tool, even with `browser_use` and `computer_use` allowed | real call, 0.163.0 only |

Not checked: what `view_image` writes for a picture that is not a small PNG;
whether Codex also searches a machine-wide folder such as `/etc/codex/skills`
(none exists on this machine); whether Codex shortens a long skills list;
whether a skill that names tool servers makes Codex start one when the skill
is used (the feature that would do it is switched off, pull request 11); web
search and compaction on 0.170.0; Linux; other models. The check after each
call (section 6) reports any of the skills cases that turns out otherwise.

### Record shapes seen in the spikes

Ids and paths here are invented stand-ins.

- **Stream, tool server call** (in the calling thread's stream only):
  `{"type":"mcp_tool_call","server":"chem","tool":"lookup","arguments":{…},"result":{"content":[{"type":"text","text":"…"}],"structured_content":null},"error":null,"status":"completed"}`.
  A refused call has `"result":null,"error":{"message":"…"},"status":"failed"`.
- **Rollout, tool server call** (in the calling thread's own rollout, lead or
  sub-agent): the script record `response_item` / `custom_tool_call` with
  `name` `exec` and an `input` such as
  `text(await tools.mcp__chem__lookup({query:"x"}));`, its
  `custom_tool_call_output`, and an `event_msg` whose payload is
  `{"type":"item_completed","thread_id":…,"turn_id":…,"item":{"type":"McpToolCall","id":"exec-1111…","server":"chem","tool":"lookup","arguments":{…},"readOnlyHint":true,"status":"completed","result":{"content":[…],"isError":false},"duration":{"secs":0,"nanos":439666}}}`.
  The item's `id` equals `_meta.callId` of the request the server received.
- **What the server receives with a call**: `params` =
  `{"name":…,"arguments":{…},"_meta":{"callId":"exec-1111…","itemId":…,"threadId":…,"sessionId":…,"windowId":…,"progressToken":…,"x-codex-turn-metadata":{"session_id":…,"thread_id":…,"turn_id":…,"thread_source":"user","model":…,"reasoning_effort":…,"sandbox":…,"sandbox_mode":…,"codex_version":…,"workspaces":{"<local path of the enclosing repository>":{"associated_remote_urls":{"origin":…},"latest_git_commit_hash":…,"has_changes":…}},…}}}`.
  For a sub-agent `thread_source` is `"subagent"` and `parent_thread_id` and
  `subagent_kind` are present (seen on 0.163.0).
- **What a URL server receives**: `POST`, `GET` and `DELETE` on its URL with
  `user-agent: codex-mcp-client/<version>`, `mcp-protocol-version`,
  `mcp-session-id`, `authorization: Bearer …` when a token variable is named,
  and the same `params` as above in the body of a call.
- **Tool names** the model calls: `mcp__<server>__<tool>`.
- **Rollout, viewed image**: the script record with an `input` such as
  `const r = await tools.view_image({path:"/w/plot.png"}); image(r.image_url);`;
  an `event_msg` with `item` `{"type":"ImageView","id":"exec-2222…","path":"file:///w/plot.png"}`
  and the thread's `turn_id`; and the `custom_tool_call_output` whose `output`
  is `[{"type":"input_text","text":"Script completed\n…"},{"type":"input_image","image_url":"data:image/png;base64,…","detail":"original"}]`.
  A refused view has only a text item: ``unable to locate image at `…`: Operation not permitted (os error 1)``.
- **Server process**: one per thread, parent is the Codex process, leader of its
  own process group, working directory the session's workspace.
- **Rollout, skills list**: among the first records of a thread's rollout,
  `{"type":"response_item","payload":{"type":"message","role":"developer","content":[{"type":"input_text","text":"<skills_instructions>\n## Skills\n…\n### Skill roots\n- `r0` = `/w/.agents/skills`\n### Available skills\n- plot-check: Checks a plot before it is reported. (file: r0/plot-check/SKILL.md)\n…</skills_instructions>"}]}}`.
  Each skill is one line: name, description, and its file under a numbered
  root. With `skills.include_instructions=false` there is no such message. A
  thread resumed after its skills changed gets another such message, holding
  the whole list again.

## Design

### 1. Settings

A role's entry under `native_capabilities` gains three optional keys. Each
appears in the stored settings only when it is switched on, so a run without
them stores exactly what it stores today.

```json
"native_capabilities": {
  "lab": {
    "terminal": true,
    "view_image": true,
    "tool_servers": {
      "chem": {"description": "Fingerprints and similarity for SMILES.",
               "command": "/opt/tools/bin/python", "args": ["-m", "chem_server"],
               "env": {"CHEM_MODE": "fast"}, "env_from": ["CHEM_API_KEY"],
               "tool_timeout_seconds": 300},
      "papers": {"description": "Full text of open-access papers.",
                 "url": "https://papers.example.org/mcp",
                 "token_from": "PAPERS_TOKEN",
                 "headers": {"X-Client": "rsi-lab"}}
    },
    "skills": {"root": "/data/research-skills"}
  }
}
```

### 2. Tool servers

- **Two kinds.** A server has either `command`, and Codex starts it for the
  call, or `url`, and Codex connects to it. Never both.
- **Declaring a local one.** `command` is an absolute path. `args` is a list of
  strings. `env` holds literal values that are not secret. `env_from` names
  variables whose values are taken from the environment the Controller runs in
  at call time; the config, the call record and the command line hold only the
  names.
- **Declaring one by URL.** `url` begins with `https://`, or with `http://`
  when the host is `localhost`, `127.0.0.1` or `[::1]`. `token_from` names one
  environment variable whose value Codex sends as a bearer token; it is
  optional, and only the name is stored. `headers` holds fixed header values
  that are not secret. A header named `Authorization` is refused. **(my call)**
- **Both kinds.** `description` is one line of 1 to 300 characters.
  `tool_timeout_seconds` is 1 to 3,600 and optional. A name matches
  `[a-z][a-z0-9_]{0,31}` and does not begin with `codex`. At most 8 servers per
  role. A variable named by `env_from` or `token_from` that is not set when a
  call is prepared refuses the call before Codex starts. **(my call on the
  limits and the refusal)**
- **On the command.** Per server, in name order. Local:
  `mcp_servers.<name>.command`, `.args`, `.env`, `.env_vars` (the `env_from`
  names). By URL: `.url`, `.bearer_token_env_var` (the `token_from` name),
  `.http_headers`. Both: `.tool_timeout_sec` when given, and
  `.default_tools_approval_mode="approve"`, without which Codex refuses every
  tool not marked read-only.
- **Who can use it.** The role and all of its sub-agents. The prompt names each
  server with its description.
- **What its results are.** Leads, like everything learned through a native
  tool. The prompt says so. The literature service of the second design is the
  one server whose results the Controller will register.
- **What it is told.** With every call Codex sends the server the thread and
  session ids, the model and the Codex version. When the run's folder lies
  inside a git repository it also sends that repository's local path, the URL
  of its remote and its latest commit. The kernel cannot switch this off. The
  documentation says so where it explains how to declare a server, and advises
  keeping the run folder outside any repository when a server belongs to
  someone else. **(my call: documented, not prevented)**
- **A URL server is a way out.** The terminal has no network, but whatever a
  role can read it can put into a call's arguments, and those leave the
  machine. So private data and credentials should not be readable by a role
  that has one, as the documentation already says of web search. A host
  records a run with one as not blind (section 9).

### 3. Stopping tool servers

- A URL server has no process; this section is about local ones.
- The process tracking the kernel uses for the terminal runs whenever a role
  has a local tool server, with or without a terminal: while Codex runs, the process
  table is sampled and every process seen under the call is tracked by pid and
  start time.
- When the call ends, however it ends, every tracked process still alive is
  stopped after a fresh check of its pid and start time. This is what reaches a
  server orphaned by a killed turn.
- The tracked processes are also written to the call's folder as they are seen,
  each with its pid and start time. If the Controller itself dies during a call,
  recovery of that call stops the ones still alive, after the same check,
  before it settles anything. **(my call)**
- A call whose processes could not all be confirmed stopped is reported the way
  a terminal call with surviving commands is today.

### 4. Image viewing

- `view_image: true` is accepted only for a role whose terminal is on;
  otherwise the config is refused. The call no longer passes
  `--disable view_image`.
- The image tool then reads only what the terminal's profile lets commands
  read: the workspace and the readable roots.
- The prompt says the role can view image files in those folders and that a
  picture it views is a lead, like any other native finding. Images the
  Controller attaches to a request keep their own rules.
- Sub-agents can view images too.

### 5. Skills

- **Declaring.** `skills.root` names a folder with one sub-folder per skill,
  each holding a `SKILL.md` whose front matter gives `name` and `description`,
  and any supporting files. The `name` is the sub-folder's name, so that what
  Codex lists can be matched to what was declared.
- **Freezing.** When the run is initialised the folder is read once: every file
  is stored in the run's own store, and the stored settings hold, per skill, its
  name, its description and its files with their hashes. Later changes to the
  folder do not reach the run.
- **Limits.** A skill name matches `[a-z][a-z0-9_-]{0,31}`. From 1 to 16
  skills; per skill at most 16 files and 262,144 bytes; one-line description of
  1 to 300 characters; regular files only, no symbolic links. Anything else,
  an empty folder included, refuses the run at initialisation. **(my call)**
- **Needs the terminal.** A skill is read with the terminal, so `skills` is
  accepted only for a role whose terminal is on.
- **Handing them over.** Before every call the kernel writes the skills afresh
  into `<workspace>/.agents/skills/<name>/`, one of the folders Codex searches.
  Commands cannot write there: the terminal's profile already makes `.agents`
  read-only. Today a terminal call is refused when the workspace holds
  `.agents`; with skills declared the kernel replaces that folder instead. The
  refusal stays for runs without skills, and for `.codex` in every run.
- **Loading.** Codex does the rest. At the start of a thread it tells the model
  the name, description and file of each skill it found, and the model opens a
  skill's file with the terminal when it decides to use one. Every sub-agent
  thread gets the same list from Codex, with or without a declared role. The
  kernel writes no list of its own, in the prompt or in a role file.
- **On the command.** For a role with skills: `skills.bundled.enabled=false`
  and `project_root_markers=[]`, so that Codex lists neither its bundled skills
  nor skills lying in folders above the workspace; and
  `skills.include_instructions=false` is not passed. Plugins stay off, as today.
  Nothing in the command depends on the owner's folders, so it is the same on
  every call. A sub-agent thread follows the same settings.

### 6. Keeping skills apart from the owner's everyday Codex

- **A role without skills is shown none.** Each of its calls passes
  `skills.include_instructions=false`, and Codex then writes no skill list at
  all. This changes the command, and what the model is shown, for every run,
  including runs with nothing switched on. It is the one deliberate exception
  to "a run that switches nothing on is unchanged".
- **A role with skills needs a Codex home with no skills of its own.** When the
  run is initialised, when it is started and before every call, the kernel
  looks at the other folders Codex would search: `skills` in the call's Codex
  home (apart from `.system`, which Codex installs itself and the command keeps
  out of the list), `$HOME/.agents/skills` and `/etc/codex/skills`.
  If any of them holds an entry the call is refused before Codex starts, and
  the message says how to set up a separate home. The check looks at folder
  entries only and records no names. **(my call)**
- **The separate home.** A folder of the owner's choosing; the owner has made
  one and logged in to it, and the 0.170.0 spike ran there. The owner logs in
  once, with `CODEX_HOME` set to the folder for `codex login`, and starts the
  Controller or a host with `CODEX_HOME` set to it, which the kernel already
  honours. The
  kernel does not create the home and never copies or links credentials into
  it. A run stays with the home it started in, because its threads live there.
  Any run may use the separate home; only a run with skills has to. Its threads
  then also stay out of the everyday Codex's history.
- **A limit.** A separate home does not help with `$HOME/.agents/skills`, which
  Codex searches whatever its home is. While that folder holds anything, a run
  with skills is refused. It does not exist on the owner's machine today.
  Lifting the limit would take the per-skill switch that was set aside under
  "Approaches".
- **The everyday home stays out of reach.** With the terminal on, the profile
  already denies commands the call's Codex home. When that is another folder
  than `~/.codex`, `~/.codex` is denied as well and a readable root inside it
  is refused. A run in a separate home then cannot read the everyday home's
  credentials, sessions or skills.
- **Nothing is written there.** Skills exist only in the run's store and its
  workspaces. The settings above are passed on the command line of one call; no
  config file is touched.
- **Checked afterwards.** After every call the kernel reads the skills lists
  Codex wrote during it: in the part of the lead's rollout the call added, and
  in each sub-agent thread the call started. A role without skills must have
  none. Under a role with skills, a thread the call started must have a list,
  and every list written during the call, in a new thread or a resumed one,
  must name exactly the declared skills, each under the workspace's
  `.agents/skills`. An extra skill, a missing one or a list the kernel cannot
  read makes the call invalid, like a stream that names an undeclared tool
  server. This is what catches a folder this design does not know Codex
  searches. **(my call)**
- **Room for writing skills later.** Codex itself tells a resumed thread when
  its skills changed, and a sub-agent sees a skill written earlier in the same
  call. So the later design needs no list written by the kernel; it will change
  what "the declared skills" means for a role allowed to write, and which part
  of `.agents` its terminal may write. Nothing here has to be undone for it.

### 7. What a call records

`native_activity` gains, for the lead:

- `tool_server_calls`: the first 32 calls, each with `server`, `tool`,
  `status`, the call id, the SHA-256 and first 200 characters of its arguments,
  the size of its result and its duration; and `tool_server_calls_omitted`.
- `images_viewed`: the first 32 file paths, with the count and total bytes of
  all images viewed.

Each `child_threads` entry gains the same two for that sub-agent, read from its
own rollout for the turns counted for this call. The call id is the one the
server received, so a server's own log can be matched to the record.

For a role with skills, `skills_shown` holds the names in the list Codex wrote
for a thread: on the lead in the call that starts its thread, and on a
`child_threads` entry in the call that starts that sub-agent. When the list is
not the declared one, the record of the invalid call gives the number of
unexpected skills and the folders they came from, never their names.

The receipt's `native_capabilities` names tool servers without their `env`
values and skills by name and hash.

**What the request carries.** The frozen settings ride in every request, where
the model sees them. For the new keys the request carries a short form: each
tool server by name and description, each skill by name only, since Codex's
list carries the descriptions. Commands, arguments, `env` values and file
hashes stay with the stored settings of the call. For a run without the new
keys the request is unchanged. **(my call)**

### 8. Reading the stream and the rollout

- The stream may hold `mcp_tool_call` items for a role with tool servers, and
  only for a declared server. An item naming any other server makes the call
  invalid. Today the audit refuses every such item.
- The rollout reader accepts the script records (`custom_tool_call` and its
  output) for a role that has tool servers or image viewing, as it does today
  for web search and the terminal.
- For a role with image viewing, a tool output may contain `input_image` items
  whose `image_url` is a `data:` URL of an image type. Such an item is accepted
  without being decoded: the kernel does not know what Codex writes for every
  picture format and size, and refusing one would make the whole session
  unreadable. Its bytes count in the session's history like any other tool
  output, so the existing limit on a request's context covers them. The separate
  limit on image bytes applies, as today, only to images the Controller
  attached; viewed images are listed apart from those. **(my call)**
- Without image viewing an image in a tool output is refused, as today.
- The rollout reader finds Codex's skills list as a `developer` message whose
  text begins with `<skills_instructions>`, and takes from it the folders and
  the skill lines for the check of section 6. A message of that kind whose
  folders or lines it cannot make out counts as a list it cannot read.

### 9. The PABLO host

- `--native-capabilities FILE` passes the three new keys through the kernel's
  validation; nothing else changes in how a team run is prepared.
- A team run with skills is started with `CODEX_HOME` set to the separate home,
  like any other run. The host needs no option of its own for it.
- The not-blind statement also covers tool servers, because a server can reach
  anything. When any role declares one, local or by URL, the run is recorded as
  not blind with a sentence of the same form that names the roles. **(my call)**
- The check that the terminal is not handed the hidden objective applies to the
  skills folder as well: a folder that exposes the scorer is refused.
  **(my call)**

### 10. Recovery and older runs

- The new keys are part of the native settings the kernel already compares on
  recovery.
- Skills and role files are written afresh before every call.
- A run prepared before this change has none of the keys. Resumed, it differs
  only by the first point of section 6. A thread it started earlier keeps the
  skills list Codex gave it then; the check of section 6 looks only at what a
  call itself wrote.
- A terminal run that already uses a separate Codex home gains the denial of
  `~/.codex` from its next call.

### 11. Tests

All offline, with fixtures built from the spike's record shapes and invented
ids: settings and their refusals, including image viewing or skills without a
terminal; the command for each key and unchanged settings without them; secrets
absent from config, records and command; a URL server's command values, its
refusals (plain `http` to another host, an `Authorization` header, both
`command` and `url`, an unset variable) and that it starts no tracking;
freezing a skills folder and each
limit; skills written and rewritten; the skills settings on the command with
and without skills; the home check for each of its three folders, and that it
records no names; the denial of `~/.codex` under a separate home; the skills
list reader for a list as declared, with an extra skill, with one missing,
unreadable, present where none is allowed, in a resumed thread that got a
second list and in a sub-agent thread; the prompt additions for tool servers and image viewing;
stream and rollout readers with and without each key, including an undeclared
server; image counting against the history limit; recorded calls and images for
lead and sub-agent; tracking switched on by a tool server alone and an orphaned
server stopped; the PABLO statement and folder check.

## After the build

The spike of 2026-11-19 drove the settings by hand through the kernel's
transport. A check through the real executor is still owed once the code
exists, and needs the owner's go-ahead: one call with a local server (a secret
by `env_from`), a URL server, an image that is not a small PNG and a skill, on
a lead and a role sub-agent, accepted by every kernel check with the record
showing the calls, the image and exactly the declared skills; a killed turn
leaving no local server behind; and an image outside the allowed folders
refused, which was last seen on 0.163.0.

## Left to the builder

- How frozen skill files are named in the store, and how the settings refer to
  them.
- Field names in records beyond those given here.
- The exact wording of the two prompt additions, and of the refusal that
  explains the separate home.
- Whether `tool_timeout_seconds` gets a default when absent.
- Whether the skills list of a resumed thread is read again from the start of
  its rollout or only looked for in the part the call added.
