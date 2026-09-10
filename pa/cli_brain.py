"""A brain that runs the model through the headless `claude` CLI.

Same contract as `Brain`: one method, `respond(transcript, state) -> Reply`.
`Conversation` calls nothing else (`conversation.py`), so the two are
interchangeable and `live.py` picks between them with an environment variable.

--- Why this exists ---

`Brain` calls the Messages API with an API key, which is a separate billing
relationship from the one office-server already has. The office has been
spawning headless `claude` for its own agents since Phase 12.2
(`agents/claudeCommand.ts`), authenticated by the CLI's own login, so the
machine on Cubie's desk can already run a model without an API key. This makes
the same thing available to the robot.

It is a different trade, not a free one:

  * A turn costs a process spawn. Measured on office-server at ~3.7 s wall for
    a trivial prompt, against roughly 2 s for a direct HTTPS POST. He gains a
    beat before answering.
  * The model's tool calls are invisible. `--output-format json` reports the
    final text and nothing about what was called along the way, which is why
    `office_mcp.py` writes a turn log and this reads it back.
  * The CLI is a coding agent by default. Left alone it brings a filesystem, a
    shell and a system prompt written for repositories.

--- The tool surface is closed on purpose ---

His brain is driven by whatever is said in the room, and he demonstrably wakes
on the television. So the invocation is locked down at three independent
layers, and each one alone would be enough:

  * `--tools ""` removes every built-in tool. No Bash, no Read, no WebFetch.
  * `--strict-mcp-config` ignores every MCP server except the one named here,
    so nothing configured for the user or a project can reach him.
  * `--setting-sources ""` loads no user, project or local settings, so no
    CLAUDE.md and no settings file can add to the above.

`--allowedTools` then names exactly the four office tools. That is the
allowlist; the three flags above are the reason the allowlist is not the only
thing standing between a stranger's voice and a shell on office-server.

--- The system prompt is replaced, not appended ---

`--system-prompt`, not `--append-system-prompt`. The office's agent runner uses
the latter because its agents ARE coding agents and want Claude Code's own
prompt underneath. Cubie is not: he wants PERSONALITY.md's voice and a strict
one-or-two-sentence answer with no markdown, and the default prompt pulls hard
in the other direction. A robot that reads asterisks aloud is worse than a
robot that says nothing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import brain as brain_mod
from brain_tools import ActionRecord

logger = logging.getLogger("cubie.brain")

#: The name the MCP server is registered under. It becomes part of every tool
#: name the model sees (`mcp__cubie__approve_request`), so it is also what
#: `--allowedTools` has to spell -- keep the two in step.
SERVER_KEY = "cubie"

#: How long one turn may take before he gives up and says so.
#:
#: A person is standing in front of him. Thirty seconds of a robot staring
#: silently is a robot that has crashed as far as anyone in the room is
#: concerned, and the speaker has to produce SOMETHING. Generous enough to
#: cover a spawn, a cold model call and a couple of tool round trips; short
#: enough that the failure is still legible as a failure.
DEFAULT_TIMEOUT_S = 25.0

#: Where `office_mcp.py` lives, so the config can name it without the CLI
#: needing to know anything about this repo's layout.
_MODULE_DIR = Path(__file__).resolve().parent

#: Environment variables removed before spawning `claude`.
#:
#: The CLI resolves these BEFORE its own stored login, and says so when it
#: does: "claude.ai connectors are disabled because ANTHROPIC_API_KEY or
#: another auth source is set and takes precedence over your claude.ai login".
#: We inherit `os.environ`, and `ANTHROPIC_API_KEY` is in the character stack's
#: environment because the same file configures the API brain -- so a key left
#: behind for `CUBIE_BRAIN=api` silently decides the credential for
#: `CUBIE_BRAIN=cli` as well.
#:
#: That is not hypothetical: a stale 10-character key in
#: /etc/cubie-character.env made every turn fail while the identical command
#: succeeded from a shell that did not have it set. Choosing the CLI brain has
#: to mean choosing the CLI's own login, so the variables that would override
#: it are dropped rather than trusted to be absent.
BLOCKED_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def child_env(environ: dict[str, str] | None = None) -> dict[str, str]:
    """The environment `claude` is spawned with. Pure, so a test can assert it."""
    source = os.environ if environ is None else environ
    return {k: v for k, v in source.items() if k not in BLOCKED_ENV}


@dataclass(frozen=True)
class _Turn:
    """What came back from one invocation, before it becomes a `Reply`."""

    text: str
    actions: tuple[ActionRecord, ...]
    face: str | None


def read_turn_log(path: Path) -> tuple[tuple[ActionRecord, ...], str | None]:
    """Replay what the MCP server recorded, and find the face it was asked for.

    Tolerant by design: a half-written final line means the subprocess died
    mid-write, and the actions before it still happened and still have to be
    reported. Losing the whole log because its tail is ragged would hide an
    approval that really took effect.
    """
    if not path.exists():
        return (), None
    actions: list[ActionRecord] = []
    face: str | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except ValueError:
            logger.warning("turn log has a ragged line, ignoring it: %.80r", line)
            continue
        record = ActionRecord(
            name=str(raw.get("name", "")),
            arguments=raw.get("arguments") or {},
            outcome=str(raw.get("outcome", "")),
            detail=str(raw.get("detail", "")),
        )
        actions.append(record)
        # Last one wins, matching `Brain`: the model may change its mind
        # mid-turn, and the face it ends on is the one it meant.
        if record.name == "set_face" and record.outcome == "ok":
            wanted = record.arguments.get("face")
            if isinstance(wanted, str):
                face = wanted
    return tuple(actions), face


class CliBrain:
    """One conversational turn, run through `claude -p`."""

    def __init__(
        self,
        office: Any,
        model: str | None = None,
        executable: str = "claude",
        timeout_s: float = DEFAULT_TIMEOUT_S,
        python: str | None = None,
    ) -> None:
        self._office = office
        #: Kept as an attribute rather than baked into the args so the startup
        #: log can name it, the way `Brain.model` already does.
        self.model = model or ""
        self.executable = executable
        self.timeout_s = timeout_s
        #: The interpreter that runs the MCP server. `sys.executable` by
        #: default, which is the gateway's -- the same one this module is
        #: running on, so `office` and `brain_tools` are importable from it.
        self.python = python or sys.executable

    # -- the contract ----------------------------------------------------

    async def respond(self, transcript: str, state: Any) -> brain_mod.Reply:
        system = brain_mod.build_system_prompt(state)
        with tempfile.TemporaryDirectory(prefix="cubie-turn-") as workdir:
            log_path = Path(workdir) / "actions.jsonl"
            turn = await self._invoke(transcript, system, log_path, workdir)
        return brain_mod.Reply(
            speech=brain_mod.trim_speech(turn.text),
            face=turn.face,
            actions=turn.actions,
            incomplete=not turn.text,
        )

    # -- the invocation --------------------------------------------------

    def mcp_config(self, log_path: Path) -> dict:
        """The `--mcp-config` payload, as data so a test can assert its shape.

        `env` carries only what the server needs: where the office is, the
        token to talk to it, where to write the log, and the path that makes
        `import office` work from a different working directory. Notably absent
        is `STACKCHAN_TOKEN` -- this server has no business reaching the
        gateway, and not passing it is cheaper than trusting it not to.
        """
        return {
            "mcpServers": {
                SERVER_KEY: {
                    "command": self.python,
                    "args": [str(_MODULE_DIR / "office_mcp.py")],
                    "env": {
                        "PYTHONPATH": str(_MODULE_DIR),
                        "CUBIE_TURN_LOG": str(log_path),
                        "OFFICE_HUB": os.environ.get("OFFICE_HUB", ""),
                        "AGENTHUB_COMPANION_TOKEN": os.environ.get(
                            "AGENTHUB_COMPANION_TOKEN", ""
                        ),
                    },
                }
            }
        }

    def build_args(self, transcript: str, system: str, log_path: Path) -> list[str]:
        """The whole command line, pure so it is table-testable.

        Same reasoning as `agents/claudeCommand.ts` in the office: the exact
        invocation -- above all the three flags that close the tool surface --
        is the part worth asserting in a test rather than reading off a running
        process.
        """
        args = [
            self.executable,
            "-p",
            transcript,
            "--system-prompt",
            system,
            # Every built-in tool removed. See the module docstring.
            "--tools",
            "",
            "--mcp-config",
            json.dumps(self.mcp_config(log_path)),
            "--strict-mcp-config",
            "--setting-sources",
            "",
            "--allowedTools",
            *[f"mcp__{SERVER_KEY}__{tool['name']}" for tool in brain_mod.TOOLS],
            "--output-format",
            "json",
            # Each turn is a fresh conversation with no memory, so persisting a
            # session per utterance would fill ~/.claude/projects with one
            # directory per thing anyone ever said to him.
            "--no-session-persistence",
        ]
        if self.model:
            args += ["--model", self.model]
        return args

    async def _invoke(
        self, transcript: str, system: str, log_path: Path, workdir: str
    ) -> _Turn:
        args = self.build_args(transcript, system, log_path)
        logger.debug("cli brain: %s", " ".join(args[:2]))

        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                # Load-bearing, and worth three seconds a turn: with an
                # inherited stdin the CLI waits for piped input before it will
                # start, announcing "no stdin data received in 3s, proceeding
                # without it". The prompt is already in argv; there is nothing
                # to pipe, and a person is waiting.
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                # A directory with nothing in it. The CLI reads the working
                # directory for context, and his brain has no business seeing a
                # repository.
                cwd=workdir,
                # Explicit, so an API key meant for the other brain cannot
                # quietly decide which credential this one uses. See BLOCKED_ENV.
                env=child_env(),
            )
        except FileNotFoundError as exc:
            raise brain_mod.BrainError(
                f"{self.executable!r} is not on PATH, so the CLI brain cannot run"
            ) from exc

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self.timeout_s
            )
        except asyncio.TimeoutError:
            process.kill()
            # Read what it managed to say before it was killed. The first
            # version of this discarded both pipes, so the one message that
            # would have explained the hang -- the CLI's own complaint on
            # stderr -- was thrown away, and diagnosing a timeout took four
            # rounds of guessing instead of reading one line. A diagnostic
            # that drops the diagnosis is worth less than none.
            said = await self._drain(process)
            actions, face = read_turn_log(log_path)
            # The actions still happened, so they are still reported -- an
            # approval that took effect must never be lost to a timeout.
            raise brain_mod.BrainError(
                f"the CLI brain did not answer within {self.timeout_s:.0f}s "
                f"({len(actions)} action(s) had already run)"
                + (f"; it said: {said}" if said else "; it said nothing")
            )

        if stderr:
            # Never spoken, always logged: the CLI writes MCP warnings here and
            # a robot reading them aloud is the failure this separation exists
            # to prevent.
            logger.debug("cli brain stderr: %s", stderr.decode(errors="replace")[:500])

        actions, face = read_turn_log(log_path)

        if process.returncode != 0:
            raise brain_mod.BrainError(
                f"claude exited {process.returncode}: "
                f"{stderr.decode(errors='replace')[:200]}"
            )

        text = self._text_from(stdout)
        return _Turn(text=text, actions=actions, face=face)

    @staticmethod
    async def _drain(process: Any, limit: int = 300) -> str:
        """Whatever a killed process left on its pipes, trimmed for one log line.

        Best-effort by construction: the process is already dead and this runs
        on a failure path, so anything that goes wrong reading it must not
        replace the timeout with a less useful exception.
        """
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=2.0)
        except (asyncio.TimeoutError, ValueError, OSError):
            return ""
        parts = []
        for name, raw in (("stderr", stderr), ("stdout", stdout)):
            text = " ".join((raw or b"").decode(errors="replace").split())
            if text:
                parts.append(f"{name}={text[:limit]!r}")
        return "; ".join(parts)

    def _text_from(self, stdout: bytes) -> str:
        """Pull the answer out of `--output-format json`.

        Strict about the shape rather than forgiving: `stdout.strip()` would
        happily speak a JSON blob, an error message or an MCP warning, and the
        one thing that must never reach the speaker is machine output.
        """
        raw = stdout.decode(errors="replace").strip()
        if not raw:
            raise brain_mod.BrainError("claude produced no output at all")
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise brain_mod.BrainError(
                f"claude did not answer with JSON: {raw[:200]!r}"
            ) from exc
        if not isinstance(payload, dict):
            raise brain_mod.BrainError("claude's JSON was not an object")
        if payload.get("is_error"):
            raise brain_mod.BrainError(
                f"claude reported an error: {str(payload.get('result'))[:200]}"
            )
        denials = payload.get("permission_denials") or []
        if denials:
            # Not fatal -- he may still have said something useful -- but it
            # means a tool he reached for was refused, which is a
            # misconfiguration rather than a conversation.
            logger.warning("cli brain: %d tool call(s) were denied", len(denials))
        result = payload.get("result")
        return result.strip() if isinstance(result, str) else ""


def available(executable: str = "claude") -> bool:
    """Whether the CLI is installed, for `live.py`'s choice of brain."""
    return shutil.which(executable) is not None
