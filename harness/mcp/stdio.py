"""stdio transport: the server is a subprocess speaking newline-delimited JSON.

The server's stderr must never reach momo's own stdout/stderr (it would scribble
over the curses TUI), so a thread copies it into a capped log file under
~/.momo-harness/mcp-logs/ and keeps the last lines in memory for /mcp log.
The process runs in its own session/process group, so shutting it down also
stops whatever it spawned (npx/uvx wrappers start the real server as a child).
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import threading
from collections import deque
from pathlib import Path

LOG_MAX_BYTES = 1_000_000
TAIL_LINES = 200


class StdioTransport:
    def __init__(self, command: str, args: list[str], env: dict[str, str],
                 cwd: str | None, log_path: Path | None = None):
        self.command, self.args, self.env, self.cwd = command, args, env, cwd
        self.log_path = log_path
        self.tail: deque[str] = deque(maxlen=TAIL_LINES)
        self.proc: subprocess.Popen | None = None
        self._write_lock = threading.Lock()
        self._closing = False

    def start(self, on_message, on_close) -> None:
        env = {**os.environ, **self.env}
        exe = shutil.which(self.command, path=env.get("PATH")) or (
            self.command if os.sep in self.command and os.access(self.command, os.X_OK) else None)
        if exe is None:
            raise ConnectionError(f"command not found: {self.command}")
        try:
            self.proc = subprocess.Popen(
                [exe, *self.args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=env, cwd=self.cwd or None,
                start_new_session=True)
        except OSError as e:
            raise ConnectionError(f"cannot start {self.command}: {e}") from None
        threading.Thread(target=self._read_stdout, args=(on_message, on_close),
                         daemon=True, name="mcp-stdout").start()
        threading.Thread(target=self._read_stderr, daemon=True, name="mcp-stderr").start()

    def _read_stdout(self, on_message, on_close) -> None:
        proc = self.proc
        for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                # Servers that log to stdout break the protocol; keep the line
                # where /mcp log shows it rather than dropping it silently.
                self._log("[stdout, not JSON] " + line[:500])
                continue
            on_message(msg)
        code = proc.wait()
        if self._closing:
            on_close("closed")
            return
        last = next((ln for ln in reversed(self.tail) if ln.strip()), "")
        on_close(f"server exited (code {code})" + (f": {last[:300]}" if last else ""))

    def _read_stderr(self) -> None:
        for raw in self.proc.stderr:
            self._log(raw.decode("utf-8", "replace").rstrip("\r\n"))

    def _log(self, line: str) -> None:
        self.tail.append(line)
        if self.log_path is None:
            return
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            if self.log_path.exists() and self.log_path.stat().st_size > LOG_MAX_BYTES:
                self.log_path.replace(self.log_path.with_suffix(".log.1"))
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    def send(self, msg: dict) -> None:
        proc = self.proc
        if proc is None or proc.poll() is not None:
            raise ConnectionError("server is not running")
        data = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
        with self._write_lock:
            try:
                proc.stdin.write(data)
                proc.stdin.flush()
            except (BrokenPipeError, ValueError, OSError) as e:
                raise ConnectionError(f"server is not running ({e})") from None

    def close(self) -> None:
        """Close stdin (the spec's polite shutdown), then SIGTERM, then SIGKILL
        the process group."""
        self._closing = True
        proc = self.proc
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.stdin.close()
        except OSError:
            pass
        for sig, wait in ((None, 1.0), (signal.SIGTERM, 2.0), (signal.SIGKILL, 2.0)):
            if sig is not None:
                try:
                    os.killpg(proc.pid, sig)
                except (ProcessLookupError, PermissionError):
                    pass
            try:
                proc.wait(wait)
                return
            except subprocess.TimeoutExpired:
                continue
