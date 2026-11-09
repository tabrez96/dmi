"""Run one agent turn inside a fresh Kata microVM.

Each run gets its own VM (its own kernel), booted from the dmi-agent image:
  - only its working directory is writable (mounted at the same path, so prompts stay valid);
    the product repo's git data and extra dirs are read-only; the rest of the host is invisible
  - read-only root filesystem; /tmp and $HOME are scratch space that vanishes with the VM
  - network: the dmi-agents bridge, locked down so the only reachable thing is the gate
  - credentials: a short-lived run token for the gate, never a real key
  - CPU and memory limits, and HQ waits for free memory before booting another VM
"""

import json
import os
import secrets
import shutil
import subprocess
import threading
import time
from pathlib import Path

from . import harness

ROOT = Path(__file__).resolve().parents[1]
INSTALLED_NETWORK = Path("/usr/local/sbin/dmi-sandbox-network")   # root-owned copy (sandbox/install-boot.sh)


def network_script():
    return INSTALLED_NETWORK if INSTALLED_NETWORK.exists() else ROOT / "sandbox" / "network.sh"


def provider_of(harness_name, model):
    return "anthropic" if harness_name == "claude" else model.split("/", 1)[0]


def mem_available_mb():
    try:
        for line in open("/proc/meminfo"):
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except OSError:
        pass
    return 0


class SandboxError(Exception):
    pass


class Sandbox:
    # A VM that just booted hasn't used its memory yet, so MemAvailable doesn't show it. Launches reserve their size
    # for this long, so two runs starting together can't both pass the check against the same free memory.
    BOOT_SETTLE_SECONDS = 60

    def __init__(self, cfg, gate, state_dir):
        s = cfg["sandbox"]
        self.image, self.network = s["image"], s["network"]
        self.gate_url = f"http://{s['gate']['ip']}:{s['gate']['port']}"
        self.gate_host = s["gate"]["ip"]
        self.default_vm = s.get("vm", {"cpus": 2, "memory_mb": 2048})
        self.headroom_mb = s.get("memory_headroom_mb", 768)
        self._memory_lock, self._booting = threading.Lock(), []     # (started at, MB) of launches still settling
        self.gate = gate
        self.runs_dir = Path(state_dir) / "runs"
        self.tools_dir = ROOT / "tools"

    # -- health --

    def check_network(self):
        r = subprocess.run(["sudo", "-n", str(network_script()), "check"], capture_output=True, text=True)
        if r.returncode != 0:
            raise SandboxError(f"sandbox network not locked down: {(r.stdout + r.stderr).strip()} "
                               "(run: sudo systemctl restart dmi-sandbox-network)")

    def check(self):
        """Fail closed: refuse to run agents unless the lockdown and the image are in place."""
        self.check_network()
        r = subprocess.run(["sudo", "-n", "nerdctl", "image", "inspect", self.image],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise SandboxError(f"image {self.image} missing (run: sandbox/build.sh)")

    def cleanup_orphans(self):
        """At HQ startup nothing should be running: remove VMs and scratch dirs a previous HQ left behind."""
        r = subprocess.run(["sudo", "-n", "nerdctl", "ps", "-a", "--format", "{{.Names}}"],
                           capture_output=True, text=True)
        names = [n for n in r.stdout.split() if n.startswith("dmi-")]
        if names:
            subprocess.run(["sudo", "-n", "nerdctl", "rm", "-f", *names], capture_output=True)
        # A VM can outlive its container (e.g. a hung run removed by hand). At startup nothing of ours
        # should be running, so any Kata hypervisor still alive is an orphan. Kill by PID: a `pkill -f`
        # pattern would also match the sudo/shell command line carrying it.
        stray = subprocess.run(["pgrep", "-f", "[q]emu-system-x86_64 -name sandbox-"],
                               capture_output=True, text=True).stdout.split()
        if stray:
            subprocess.run(["sudo", "-n", "kill", "-KILL", *stray], capture_output=True)
            names += stray
        dirs = list(self.runs_dir.glob("*")) if self.runs_dir.exists() else []
        for d in dirs:
            shutil.rmtree(d, ignore_errors=True)
        return len(names), len(dirs)

    # -- one run --

    def run(self, harness_name, model, role_cfg, system, prompt, cwd, log_path, label, add_dirs=()):
        cwd, log_path = Path(cwd).resolve(), Path(log_path)
        vm = {**self.default_vm, **role_cfg.get("vm", {})}
        self.check_network()   # every run: never boot a VM onto an open network
        self._wait_for_memory(vm["memory_mb"])
        timeout = role_cfg.get("timeout_minutes", 30) * 60
        run_id = secrets.token_hex(4)
        name = f"dmi-{run_id}"
        run_dir = self.runs_dir / run_id
        token = None
        # A worktree's .git file says where its git metadata lives, and HQ runs git on the host after the run.
        # It can't be mounted read-only (Kata empties a file mounted inside another mount), so it's put back.
        dotgit = cwd / ".git"
        saved_dotgit = dotgit.read_bytes() if dotgit.is_file() and not dotgit.is_symlink() else None
        try:
            run_dir.mkdir(parents=True, exist_ok=True)
            token = self.gate.issue(label, {provider_of(harness_name, model)}, web=role_cfg.get("web", False),
                                    packages=role_cfg.get("packages", False), ttl=timeout + 300,
                                    max_requests=role_cfg.get("max_requests", 600))
            cmd, via_stdin = harness.build_command(harness_name, model, role_cfg, system, add_dirs)
            if not via_stdin:
                cmd.append(prompt)
            (run_dir / "models.json").write_text(json.dumps(self._pi_models()))
            args = self._nerdctl_args(name, cwd, run_dir, add_dirs, vm, token, harness_name, role_cfg, via_stdin)
            # stdin never reaches end-of-file through the VM's stdio forwarding, so claude's prompt is read
            # from a file instead, and pi (prompt as an argument, but it waits on an open stdin) gets /dev/null.
            (run_dir / "prompt.txt").write_text(prompt)
            launch = 'exec "$@" </dmi-run/prompt.txt' if via_stdin else 'exec "$@" </dev/null'
            wrapper = ["sh", "-c", 'mkdir -p "$HOME/.pi/agent" && cp /dmi-run/models.json "$HOME/.pi/agent/" '
                                   f'&& {launch}', "--", *cmd]
            with open(log_path, "w") as log:
                log.write(f"$ [microVM {name}, {vm['cpus']} cpu, {vm['memory_mb']} MB] {cmd[0]} --model {model} "
                          f"(cwd={cwd})\n\n--- prompt ---\n{prompt}\n\n--- output ---\n")
                log.flush()
                try:
                    p = subprocess.run(args + wrapper, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
                except subprocess.TimeoutExpired:
                    subprocess.run(["sudo", "-n", "nerdctl", "rm", "-f", name], capture_output=True)
                    log.write(f"\n\n[HQ] timed out after {timeout}s; VM destroyed\n")
                    return harness.RunOutcome(exit_code=124, cost_usd=None, timed_out=True)
                log.write(p.stdout)
                noise = [ln for ln in p.stderr.splitlines() if "level=warning" not in ln
                         and "forward signal child exited" not in ln]
                if noise:
                    log.write("\n--- stderr ---\n" + "\n".join(noise) + "\n")
            return harness.RunOutcome(exit_code=p.returncode, cost_usd=harness._cost(harness_name, p.stdout),
                                      text=harness._text(harness_name, p.stdout))
        finally:
            if token is not None:
                self.gate.revoke(token)
            shutil.rmtree(run_dir, ignore_errors=True)
            if saved_dotgit is not None:
                self._restore_file(dotgit, saved_dotgit)

    def _wait_for_memory(self, need_mb, max_wait=900):
        """Wait until there is room for a VM of `need_mb`, then reserve it while the VM boots."""
        deadline = time.time() + max_wait
        while True:
            with self._memory_lock:
                now = time.time()
                self._booting = [(started, mb) for started, mb in self._booting
                                 if now - started < self.BOOT_SETTLE_SECONDS]
                booting_mb = sum(mb for _, mb in self._booting)
                if mem_available_mb() - booting_mb >= need_mb + self.headroom_mb:
                    self._booting.append((now, need_mb))
                    return
            if time.time() > deadline:
                raise SandboxError(f"not enough free memory for a {need_mb} MB VM after {max_wait}s")
            time.sleep(5)

    def _pi_models(self):
        """Point pi's built-in providers at the gate; the run token is the API key."""
        return {"providers": {p: {"baseUrl": f"{self.gate_url}/llm/{p}/v1", "apiKey": "$DMI_RUN_TOKEN"}
                              for p in ("nvidia", "openrouter")}}

    def _nerdctl_args(self, name, cwd, run_dir, add_dirs, vm, token, harness_name, role_cfg, via_stdin):
        args = ["sudo", "-n", "nerdctl", "run", "--rm", "--name", name,
                "--runtime", "io.containerd.kata.v2", "--network", self.network,
                # No DNS: lookups fail instantly instead of waiting out timeouts on dropped packets.
                # (Everything an agent needs is reached by the gate's IP address.)
                "--dns", "127.0.0.1",
                "--cpus", str(vm["cpus"]), "--memory", f"{vm['memory_mb']}m",
                "--read-only", "--tmpfs", "/tmp:rw,exec,size=1g", "--tmpfs", "/home/agent:rw,exec,size=1g,mode=1777",
                "--security-opt", "no-new-privileges", "--cap-drop", "ALL",
                "-w", str(cwd),
                "-v", f"{cwd}:{cwd}:rw",
                "-v", f"{run_dir}:/dmi-run:ro",
                "-v", f"{self.tools_dir}:{self.tools_dir}:ro"]
        git_common = self._git_common_dir(cwd)
        if git_common:
            args += ["-v", f"{git_common}:{git_common}:ro"]
        for d in add_dirs:
            d = Path(d).resolve()
            if d.exists() and d != cwd:
                args += ["-v", f"{d}:{d}:ro"]
        env = {"HOME": "/home/agent", "DMI_GATE": self.gate_url, "DMI_RUN_TOKEN": token, "PI_OFFLINE": "1",
               "PATH": f"{self.tools_dir}:/opt/swift/usr/bin:/usr/local/bin:/usr/bin:/bin"}
        if harness_name == "claude":
            env.update({"ANTHROPIC_BASE_URL": f"{self.gate_url}/llm/anthropic", "ANTHROPIC_AUTH_TOKEN": token,
                        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_AUTOUPDATER": "1",
                        "DISABLE_TELEMETRY": "1", "DISABLE_ERROR_REPORTING": "1"})
        if role_cfg.get("packages"):
            proxy = f"http://dmi:{token}@{self.gate_host}:{self.gate_url.rsplit(':', 1)[1]}"
            env.update({"HTTPS_PROXY": proxy, "HTTP_PROXY": proxy, "https_proxy": proxy, "http_proxy": proxy,
                        "NO_PROXY": self.gate_host, "no_proxy": self.gate_host})
        for k, v in env.items():
            args += ["-e", f"{k}={v}"]
        return args + [self.image]

    @staticmethod
    def _restore_file(path, content):
        """Put `path` back as a plain file holding `content`, whatever the run turned it into."""
        if not path.is_symlink() and path.is_file() and path.read_bytes() == content:
            return
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        elif path.exists() or path.is_symlink():
            path.unlink()
        path.write_bytes(content)

    @staticmethod
    def _git_common_dir(cwd):
        """A worktree's .git file points into the repo's .git dir; mount that read-only so git works."""
        dotgit = cwd / ".git"
        if dotgit.is_file():
            line = dotgit.read_text().strip()
            if line.startswith("gitdir:"):
                wt = Path(line.split(":", 1)[1].strip())
                return wt.parent.parent if wt.parent.name == "worktrees" else wt
        return None
