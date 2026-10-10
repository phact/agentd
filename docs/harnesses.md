# Harnesses

`harness=` picks who runs the agent loop. `"ptc"` (default) is agentd's own loop above; `"claude-code"`, `"codex"`, `"opencode"` ([OpenCode](https://opencode.ai)) and `"omp"` ([oh-my-pi](https://github.com/can1357/oh-my-pi)) run the real CLI agent, with its own tools, **entirely inside the sandbox**.

```python
from agentd import patch_openai_with_ptc
from openai import OpenAI

client = patch_openai_with_ptc(OpenAI(), cwd="./workspace", harness="claude-code")

msgs = [{"role": "user", "content": "Add a test for utils.py and run it"}]
r = client.chat.completions.create(model="claude-sonnet-5", messages=msgs)

# Same conversation, now Codex (per-call override)
msgs += [{"role": "assistant", "content": r.choices[0].message.content},
         {"role": "user", "content": "Review that test"}]
r = client.chat.completions.create(model=None, messages=msgs, harness="codex")
```

**Conversations:** the `messages` you send are the history. If they match the previous exchange on the same harness, agentd resumes that harness's native session (with full tool context); otherwise, e.g. after switching harness, it starts a new session seeded with the history. Every harness shares the client's sandbox, so files and processes survive a switch.
- **Responses API:** `client.responses.create(harness=..., input=..., instructions=...)` works too, including `previous_response_id` and `stream=True` (standard `response.*` events). Give harnesses tools via `mcp_servers=` (they appear as skills), not `tools=`. PTC (`harness="ptc"`) uses the same response store (`~/.agentd/responses`): `previous_response_id` replays the stored conversation, including PTC's code rounds, to the model as input, so you can switch between PTC and a harness mid-conversation in either direction. Ids agentd didn't issue, such as a response stored by OpenAI, are passed through to the provider.
- **Tool calls in streams:** every tool the harness runs (shell commands, file reads/edits, skills, web search) is streamed the same way PTC streams its code executions: a completed `code_interpreter_call` item with the tool name, the command or arguments, and the output, which `display_events` renders as `CodeExecution`. They already ran in the sandbox; clients never execute them. In Responses streams they are separate output items between the assistant `message` items.
- **Stopping:** abandoning a stream (`break`, `close()`, cancelling the task) stops the turn. Codex, OpenCode and omp: the CLI and everything it started are killed in the sandbox. Claude Code: the turn is interrupted, and its process and background tasks keep running (see below).
- **Claude Code sessions:** a conversation keeps one `claude` process in the sandbox (`--input-format stream-json`): each turn is a message on its stdin, so background tasks (`Bash` with `run_in_background`, background agents) outlive the turn that started them. A new question interrupts a turn still running, rather than ending the process. When a background task finishes, the CLI starts a turn on its own. Each such turn goes to `patch_openai_with_ptc(..., on_unprompted=callback)` as a Responses event stream, like `responses.create(stream=True)`'s. It's recorded in the conversation, so its response id works as `previous_response_id`, and `agentd.unprompted` is true. A question sent during an unprompted turn waits for it. A model change is applied to the running process (`set_model`). A process idle for `idle_minutes` (default 10) with no background tasks is closed; the next turn resumes the session with `--resume`. The system prompt is fixed for the life of a session, so per-turn context (the time, news) belongs in the user message. `harness_options={"claude-code": {"persistent": False}}` runs each turn as its own `claude -p` again.
- **Models:** pass the harness's model (`claude-*` for Claude Code, OpenAI models for Codex), or `None` for its default. A model from the other vendor falls back to the default, so switching keeps working. OpenCode and omp take any model: `claude-*` goes through agentd's Anthropic endpoint, an OpenAI model needs `OPENAI_API_KEY` on the host, and any OpenAI-compatible server works through an `upstream` (they need a `model=`, there's no default):
  ```python
  sabik = ModelUpstream("http://10.0.2.58:8001/v1", api="chat")
  client = patch_openai_with_ptc(OpenAI(), harness="opencode", harness_options={
      "opencode": {"upstream": sabik, "model": "Qwen/Qwen3.8-27B"},
      "omp":      {"upstream": sabik, "model": "Qwen/Qwen3.8-27B", "config": {"memory": {"backend": "off"}}},
  })
  ```
  `config` is merged into OpenCode's config (`OPENCODE_CONFIG_CONTENT`) or passed to omp as a `--config` overlay. OpenCode runs with auto-update, model-catalog fetches, LSP downloads and sharing off (the sandbox has no network).
- **Transcripts:** each harness's sessions are stored in `~/.agentd/transcripts/<harness>/<workspace>/`, mounted into the sandbox where the CLI expects them (the sandbox sees only this workspace's sessions). After every turn they're copied to the CLI's usual place, `~/.claude/projects/<workspace>/`, `~/.codex/sessions/` and `~/.omp/agent/sessions/--<workspace>--/`, so `claude --resume` / `codex resume` / `omp --resume` on the host find them. OpenCode keeps sessions in a SQLite database, so its store holds one `opencode export` JSON per session (agentd imports it back to resume in a fresh sandbox; `opencode import FILE` brings one into your own OpenCode). `KrunExecutor(transcripts_dir=..., sync_transcripts=False)` changes the store root or turns the copy off. agentd also writes its own JSONL log of every turn (`AGENTD_LOG_DIR`, default `./logs`).
- **Resume by id:** every response carries `agentd.session_id`, the native session id. Pass it back as `session_id=` to resume that session, even from a new process or a fresh sandbox (if you continued it on the host, the newer copy is used). `previous_response_id` works across restarts too.
- **Instructions:** Claude Code and Codex fix a session's system prompt when it starts; resuming ignores new ones. So when a turn's `instructions` (or system message) differ from those its session started with, agentd starts a new native session seeded with the conversation, and later turns with the same instructions resume that one. With `previous_response_id`, omitted `instructions` keep the conversation's (unlike OpenAI's API, where they lapse), so the native session continues; `instructions=""` clears them.
- **Codex on another model server:** point Codex at any OpenAI-compatible server (a LAN box, vLLM, ...) and set Codex config:
  ```python
  from agentd import ModelUpstream
  client = patch_openai_with_ptc(OpenAI(), harness="codex", harness_options={"codex": {
      "upstream": ModelUpstream("http://10.0.2.58:8080/v1", api="chat"),   # or api="responses"
      "model": "qwen3-coder",                                            # default; a call's model= wins
      "config": {"web_search": "disabled", "features": {"multi_agent": False}},
  }})
  ```
  The sandbox still has no network: agentd proxies the server from the host (adding `api_key=` / `api_key_env=` there) on its own sandbox-side port. Codex only speaks the Responses API, so for `api="chat"` servers the proxy translates to chat completions (function tools included; the model's reply isn't streamed token by token, which Codex doesn't show anyway). `config` keys are Codex's `config.toml` settings, passed as `-c` flags each turn.
- **Lifecycle:** one sandbox per client (executor), started on first use and stopped by `executor.close()`; conversations and harnesses share it. Sessions outlive it: their transcripts are on the host.
- **What's available:** `agentd.available(executor_or_client)` reports, per harness, whether it's ready (its CLI is in the sandbox image and it has a working route to a model) and why not, its default model, and its models: the Anthropic models API for Claude Code, ChatGPT's (or with `OPENAI_API_KEY`, OpenAI's) list for Codex, and an upstream's `/v1/models`; lists come from the same credentials the model proxies use and are cached for 10 minutes.
  ```python
  for name, h in agentd.available(client).items():     # or available(KrunExecutor()), available_async(...)
      print(name, h.ready, h.reasons, h.default_model, [m.id for m in h.models])
  ```
  `default_model=None` means the harness picks (Claude Code; Codex with an API key). `agentd serve` answers the same at `GET /v1/harnesses`, and `GET /v1/models` is an OpenAI-style list (so `client.models.list()` works) where each model names the ready harnesses that can run it.

## Credentials

Real credentials never enter the sandbox. Inside it, each harness talks to a sandbox-side endpoint and holds only a placeholder; agentd's host-side model proxy replaces it with the real credential and forwards to the provider:

| Harness | Credential (on the host) |
|---|---|
| Claude Code, PTC with `claude-*` | `ANTHROPIC_API_KEY`, else your Claude Code login (macOS Keychain / `~/.claude/.credentials.json`) |
| Codex | `OPENAI_API_KEY`, else your Codex ChatGPT login (`~/.codex/auth.json`) |

Subscription tokens are only read, never refreshed by agentd (refreshing would log out your own CLI); run the CLI on the host to refresh them. Codex's ChatGPT mode only talks to `https://chatgpt.com`, so inside the sandbox agentd serves that hostname over TLS with a certificate from a local agentd CA (`~/.agentd/ca`) that only sandboxes trust.

