"""Retained subprocess shell backend.

One logical terminal handle owns exactly one long-lived shell process connected to a PTY. Every
call reuses that same process, so ``cd``, exported variables, activated virtualenvs, shell
options, and foreground REPLs persist across calls. The process runs as a session leader with the
PTY as its controlling terminal so job control is enabled and an interrupt reaches only the
foreground command's process group — never the shell itself.

The service HTTP/MCP process is *not* run inside this backend; the backend is a command-execution
substrate owned by the supervised service.
"""

import asyncio
import codecs
import contextlib
import errno
import fcntl
import os
import re
import signal
import struct
import subprocess
import termios
import threading
import time
from typing import Optional

from idegym.plugins.openhands.api.errors import ErrorCode, ServiceError
from idegym.plugins.openhands.api.models import TerminalBackend
from idegym.plugins.openhands.runtime.terminal.backend import (
    SPECIAL_KEYS,
    BackendExec,
    BackendHealth,
    TerminalBackendSession,
    build_command_line,
    new_token,
    sentinel_regex,
)

# Shell init: enable job control so each command runs in its own foreground process group,
# silence prompts, and drop history/bracketed-paste noise from captured output.
_INIT = (
    "set -m 2>/dev/null; export PS1='' PS2='' PROMPT_COMMAND=''; "
    "set +o history 2>/dev/null; bind 'set enable-bracketed-paste off' 2>/dev/null; true\n"
)

_POLL_INTERVAL = 0.02
_CAPTURE_CHARS = 8192


class SubprocessBackendSession(TerminalBackendSession):
    backend = TerminalBackend.SUBPROCESS
    capture_supported = True

    def __init__(
        self,
        *,
        shell: str,
        cwd: str,
        env: dict[str, str],
        cols: int = 120,
        rows: int = 40,
    ) -> None:
        self._shell = shell
        self._cwd = cwd
        self._env = dict(env)
        self._env.setdefault("TERM", "dumb")
        self._cols = cols
        self._rows = rows

        self._proc: Optional[subprocess.Popen] = None
        self._master_fd: int = -1
        self._pgid: int = -1

        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._text = ""
        self._consumed = 0
        self._lock = threading.Lock()
        self._reader: Optional[threading.Thread] = None
        self._dead = False

        self._pending_token: Optional[str] = None
        self._sentinel: Optional[re.Pattern[str]] = None
        # Set by interrupt(): a SIGINT'd command's trailing sentinel may never print (bash aborts the
        # command list on some platforms), so a pending drain must return promptly instead of hanging.
        self._interrupt_flag = False

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        await asyncio.to_thread(self._start_blocking)
        # Prime the shell (job control, quiet prompt) and swallow the init echo/output.
        self._write(_INIT.encode())
        await asyncio.sleep(0.05)
        with self._lock:
            self._consumed = len(self._text)

    def _start_blocking(self) -> None:
        master_fd, slave_fd = os.openpty()
        self._configure_tty(slave_fd)

        def _preexec() -> None:
            os.setsid()
            with contextlib.suppress(OSError):
                fcntl.ioctl(0, termios.TIOCSCTTY, 0)

        self._proc = subprocess.Popen(
            [self._shell, "--norc", "--noprofile"],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=self._cwd,
            env=self._env,
            preexec_fn=_preexec,
            close_fds=True,
        )
        os.close(slave_fd)
        self._master_fd = master_fd
        self._pgid = os.getpgid(self._proc.pid)
        self._reader = threading.Thread(target=self._read_loop, name="oh-subproc-reader", daemon=True)
        self._reader.start()

    def _configure_tty(self, fd: int) -> None:
        attrs = termios.tcgetattr(fd)
        # lflag: drop echo so our writes are not reflected into captured output.
        attrs[3] &= ~(termios.ECHO | termios.ECHONL)
        # oflag: drop output post-processing (no \n -> \r\n) for clean line endings.
        attrs[1] &= ~termios.OPOST
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        with contextlib.suppress(OSError):
            winsize = struct.pack("HHHH", self._rows, self._cols, 0, 0)
            fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)

    def _read_loop(self) -> None:
        while True:
            try:
                raw = os.read(self._master_fd, 65536)
            except OSError as ex:
                if ex.errno == errno.EIO:  # slave closed: shell exited
                    raw = b""
                else:
                    raw = b""
            if not raw:
                self._dead = True
                return
            chunk = self._decoder.decode(raw)
            if chunk:
                with self._lock:
                    self._text += chunk

    def _write(self, data: bytes) -> None:
        if self._master_fd >= 0 and not self._dead:
            with contextlib.suppress(OSError):
                os.write(self._master_fd, data)

    # -- reading / sentinel parsing ----------------------------------------

    def _take(self, sentinel: re.Pattern[str]) -> tuple[str, Optional[int], Optional[str], bool]:
        """Consume new output; if the sentinel is complete, also return exit code + cwd."""
        with self._lock:
            pending = self._text[self._consumed :]
            m = sentinel.search(pending)
            if m:
                before = pending[: m.start()]
                self._consumed += m.end()
                return self._clean(before), int(m.group(1)), m.group(2).strip(), True
            # No sentinel yet: emit whole lines only, retaining the trailing partial line so a
            # sentinel that has not fully arrived is never split across two reads.
            cut = pending.rfind("\n")
            if cut <= 0:
                return "", None, None, False
            before = pending[:cut]
            self._consumed += cut
            return self._clean(before), None, None, False

    @staticmethod
    def _clean(text: str) -> str:
        # Drop the echoed command line (it embeds the sentinel format string) and stray CRs.
        lines = [ln for ln in text.split("\n") if "__IDEGYM_OH_END__" not in ln]
        return "\n".join(lines).replace("\r", "")

    async def _resync(self) -> BackendExec:
        """Recover the prompt after an interrupt.

        The interrupted command's trailing sentinel may never print (bash aborts the command list on
        SIGINT on some platforms), so inject a fresh sentinel that runs as soon as the shell is idle,
        discard everything emitted before it, and clear the pending command.
        """
        token = new_token()
        sentinel = sentinel_regex(token)
        self._write(build_command_line("true", token).encode())
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            _before, rc, cwd, done = self._take(sentinel)  # discard residual output from the abort
            if done:
                self._pending_token = None
                self._sentinel = None
                return BackendExec(output="", running=False, exit_code=130, cwd=cwd, interrupted=True)
            if self._dead:
                return BackendExec(running=False, lost=True)
            await asyncio.sleep(_POLL_INTERVAL)
        # The command ignored the signal and is still running; leave the handle marked running.
        return BackendExec(running=True, interrupted=True)

    async def _drain(self, sentinel: re.Pattern[str], timeout: float) -> BackendExec:
        deadline = time.monotonic() + max(0.0, timeout)
        collected: list[str] = []
        while True:
            if self._interrupt_flag:
                self._interrupt_flag = False
                return await self._resync()
            before, rc, cwd, done = self._take(sentinel)
            if before:
                collected.append(before)
            if done:
                return BackendExec(output="\n".join(collected), running=False, exit_code=rc, cwd=cwd)
            if self._dead:
                return BackendExec(output="\n".join(collected), running=False, lost=True)
            if time.monotonic() >= deadline:
                return BackendExec(output="\n".join(collected), running=True)
            await asyncio.sleep(_POLL_INTERVAL)

    async def _read_window(self, timeout: float) -> BackendExec:
        deadline = time.monotonic() + max(0.0, timeout)
        collected: list[str] = []
        while time.monotonic() < deadline:
            if self._interrupt_flag:
                self._interrupt_flag = False
                with self._lock:
                    pending = self._text[self._consumed :]
                    self._consumed = len(self._text)
                if pending:
                    collected.append(self._clean(pending))
                return BackendExec(output="".join(collected), running=False, exit_code=130, interrupted=True)
            with self._lock:
                pending = self._text[self._consumed :]
                self._consumed = len(self._text)
            if pending:
                collected.append(self._clean(pending))
            if self._dead:
                return BackendExec(output="".join(collected), running=False, lost=True)
            await asyncio.sleep(_POLL_INTERVAL)
        return BackendExec(output="".join(collected), running=False)

    # -- operations ---------------------------------------------------------

    async def execute(self, command: str, timeout: float) -> BackendExec:
        if self._dead:
            return BackendExec(lost=True)
        self._interrupt_flag = False
        token = new_token()
        self._pending_token = token
        self._sentinel = sentinel_regex(token)
        self._write(build_command_line(command, token).encode())
        res = await self._drain(self._sentinel, timeout)
        if not res.running:
            self._pending_token = None
        return res

    async def input(self, text: str, timeout: float) -> BackendExec:
        if self._dead:
            return BackendExec(lost=True)
        # Defense in depth: never write text to an idle shell (it would start an untracked command
        # with no sentinel and corrupt protocol state). The manager rejects this first.
        if not self.has_foreground_command:
            raise ServiceError(
                ErrorCode.TERMINAL_NOT_RUNNING,
                "No foreground command is active; terminal input requires a running command",
            )
        if text == "C-c":
            await self.interrupt()
        elif text in SPECIAL_KEYS:
            self._write(SPECIAL_KEYS[text])
        else:
            self._write((text + "\n").encode())
        if self._pending_token and self._sentinel is not None:
            res = await self._drain(self._sentinel, timeout)
            if not res.running:
                self._pending_token = None
            if text == "C-c" and res.exit_code is not None:
                res.interrupted = True
            return res
        return await self._read_window(min(timeout, 0.3))

    async def poll(self, timeout: float) -> BackendExec:
        if self._dead:
            return BackendExec(lost=True)
        if self._pending_token and self._sentinel is not None:
            res = await self._drain(self._sentinel, timeout)
            if not res.running:
                self._pending_token = None
            return res
        return await self._read_window(min(timeout, 0.3))

    async def interrupt(self) -> None:
        if self._dead or self._master_fd < 0:
            return
        await asyncio.to_thread(self._interrupt_blocking)

    def _interrupt_blocking(self) -> None:
        # Mark the foreground command interrupted so a pending drain returns promptly: on some
        # platforms bash aborts the command list on SIGINT, so its trailing sentinel never prints.
        self._interrupt_flag = True
        # Preferred path: when job control is active the foreground command has its own process
        # group; signal only it so the shell survives. This is what happens on macOS.
        try:
            fg = os.tcgetpgrp(self._master_fd)
        except OSError:
            fg = -1
        if fg > 0 and fg != self._pgid:
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(fg, signal.SIGINT)
            return
        # Fallback (e.g. Linux, where non-interactive job control may not isolate the command):
        # SIGINT the shell's descendant processes directly, leaving the shell itself running.
        self._signal_shell_children(signal.SIGINT)

    def _signal_shell_children(self, sig: int) -> bool:
        """Send ``sig`` to the shell's direct child processes (not the shell). Returns True if any."""
        if self._proc is None:
            return False
        signalled = False
        for pid in self._shell_children():
            # Signal the child PID directly, never its process group: when job control is not
            # isolating the command, the child shares the shell's group and killpg would take the
            # shell down too.
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.kill(pid, sig)
                signalled = True
        return signalled

    def _shell_children(self) -> list[int]:
        pid = self._proc.pid
        # Prefer /proc (Linux, no extra packages); fall back to pgrep where /proc is unavailable.
        try:
            with open(f"/proc/{pid}/task/{pid}/children") as handle:
                return [int(p) for p in handle.read().split() if p.isdigit()]
        except OSError:
            pass
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            out = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True, timeout=2)
            return [int(p) for p in out.stdout.split() if p.strip().isdigit()]
        return []

    async def capture(self) -> str:
        with self._lock:
            tail = self._text[-_CAPTURE_CHARS:]
        return self._clean(tail)

    async def health(self) -> BackendHealth:
        alive = self.alive
        return BackendHealth(
            backend=self.backend,
            alive=alive,
            detail={"shell": self._shell, "pid": self._proc.pid if self._proc else None},
        )

    async def close(self) -> None:
        proc = self._proc
        if proc is None:
            return
        # Terminate the whole owned process group.
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(self._pgid, signal.SIGTERM)
        try:
            await asyncio.to_thread(proc.wait, 2.0)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(self._pgid, signal.SIGKILL)
            with contextlib.suppress(Exception):
                await asyncio.to_thread(proc.wait, 2.0)
        self._dead = True
        if self._master_fd >= 0:
            with contextlib.suppress(OSError):
                os.close(self._master_fd)
            self._master_fd = -1

    @property
    def alive(self) -> bool:
        return not self._dead and self._proc is not None and self._proc.poll() is None

    @property
    def has_foreground_command(self) -> bool:
        return self._pending_token is not None
