"""Launch one headless agent run: a harness CLI + a model, in a working directory.

Every harness follows the same contract: it gets a system prompt (the role
charter) and a task prompt, works inside `cwd`, and leaves `.dmi/result.json`
behind. HQ reads that file; stdout only goes to the log.
"""

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


# What every agent process may see: enough to run a CLI, nothing secret.
BASE_ENV = ["PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "TERM", "TZ", "TMPDIR",
            "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME",
            "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR"]
# Model provider prefix (pi's "provider/model") -> the key that provider needs.
PROVIDER_KEYS = {
    "nvidia": "NVIDIA_API_KEY", "openrouter": "OPENROUTER_API_KEY", "zai": "ZAI_API_KEY",
    "moonshotai": "MOONSHOT_API_KEY", "kimi-coding": "KIMI_API_KEY",
    "qwen-token-plan": "QWEN_TOKEN_PLAN_API_KEY", "fireworks": "FIREWORKS_API_KEY",
    "together": "TOGETHER_API_KEY", "groq": "GROQ_API_KEY", "cerebras": "CEREBRAS_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "google": "GEMINI_API_KEY",
}
# Claude runs on the host (chat) use CLAUDE_CODE_OAUTH_TOKEN when it's set, so every agent uses the
# same account as the gate; without it they fall back to this machine's own `claude` login.
CLAUDE_KEYS = ["CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
               "CLAUDE_CONFIG_DIR"]


def agent_env(harness, model, role_cfg, source=None):
    """The environment for one agent run: an allowlist, never HQ's whole environment.

    Discord tokens and keys for other providers never reach an agent. Extra
    variables a role needs (e.g. the scout's BRAVE_API_KEY) go in its `secrets` list.
    """
    source = os.environ if source is None else source
    names = list(BASE_ENV) + list(role_cfg.get("secrets", []))
    if harness == "claude":
        names += CLAUDE_KEYS
    elif harness == "pi":
        names += [k for k in source if k.startswith("PI_")]
        key = PROVIDER_KEYS.get(model.split("/", 1)[0])
        if key:
            names.append(key)
    return {n: source[n] for n in names if n in source}


@dataclass
class RunOutcome:
    exit_code: int
    cost_usd: float | None
    timed_out: bool = False
    text: str = ""          # the agent's final reply (used by chat, which may have no tools)


def parse_reply(text):
    """A JSON object from an agent's final text (code fences and chatter around it are fine).
    Plain text with no JSON becomes {"message": text}."""
    t = (text or "").strip()
    if not t:
        return None
    start, end = t.find("{"), t.rfind("}")
    if start != -1 and end > start:
        try:
            data = json.loads(t[start:end + 1])
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    return {"message": t}


def build_command(harness, model, role_cfg, system, add_dirs=()):
    tools = role_cfg.get("tools", [])
    if harness == "claude":
        cmd = ["claude", "-p", "--model", model,
               "--append-system-prompt", system,
               "--output-format", "json",
               "--permission-mode", role_cfg.get("permission_mode", "acceptEdits"),
               "--no-session-persistence",
               # Agents never get the PM's own Claude setup: no MCP connectors (Slack, Gmail...),
               # no skills or slash commands. Only the tools listed for the role.
               "--strict-mcp-config", "--disable-slash-commands"]
        if "max_usd_per_run" in role_cfg:
            cmd += ["--max-budget-usd", str(role_cfg["max_usd_per_run"])]
        if role_cfg.get("no_tools"):
            cmd += ["--tools", ""]
        elif tools:
            cmd += ["--tools", ",".join(tools), "--allowedTools", ",".join(tools)]
        for d in add_dirs:
            cmd += ["--add-dir", str(d)]
        return cmd, True  # prompt goes on stdin
    if harness == "pi":
        cmd = ["pi", "-p", "--model", model,
               "--append-system-prompt", system,
               "--no-session", "--no-approve",
               "--no-extensions", "--no-skills"]          # nothing from the PM's own pi setup
        if role_cfg.get("no_tools"):
            cmd += ["--no-tools", "--no-context-files"]
        elif tools:
            cmd += ["--tools", ",".join(tools)]
        if "thinking" in role_cfg:
            cmd += ["--thinking", role_cfg["thinking"]]
        return cmd, False  # prompt goes last, as an argument
    raise ValueError(f"unknown harness {harness!r}")


def run(harness, model, role_cfg, system, prompt, cwd, log_path, role=None, subject=None,
        add_dirs=()):
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if harness == "fake":
        from . import fake_agents              # the offline stand-in (tests, safety.pipeline: fake)
        return fake_agents.run(role, subject, prompt, Path(cwd), log_path)

    cmd, via_stdin = build_command(harness, model, role_cfg, system, add_dirs)
    if not via_stdin:
        cmd.append(prompt)
    timeout = role_cfg.get("timeout_minutes", 30) * 60
    with open(log_path, "w") as log:
        log.write(f"$ {cmd[0]} --model {model} (cwd={cwd})\n\n--- prompt ---\n{prompt}\n\n--- output ---\n")
        log.flush()
        try:
            p = subprocess.run(cmd, cwd=cwd, input=prompt if via_stdin else None,
                               stdin=None if via_stdin else subprocess.DEVNULL,   # pi waits on an open stdin
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, timeout=timeout, env=agent_env(harness, model, role_cfg))
        except subprocess.TimeoutExpired as e:
            log.write((e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or ""))
            log.write(f"\n\n[HQ] timed out after {timeout}s\n")
            return RunOutcome(exit_code=124, cost_usd=None, timed_out=True)
        log.write(p.stdout)
    return RunOutcome(exit_code=p.returncode, cost_usd=_cost(harness, p.stdout), text=_text(harness, p.stdout))


def _claude_json(stdout):
    try:
        data = json.loads(stdout)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _cost(harness, stdout):
    return _claude_json(stdout).get("total_cost_usd") if harness == "claude" else None


def _text(harness, stdout):
    return (_claude_json(stdout).get("result") or "") if harness == "claude" else stdout
