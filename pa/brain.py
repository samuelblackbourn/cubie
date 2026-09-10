"""The brain: what Cubie says, and what he does about it.

One turn is: a transcript in, the office's state as context, and out comes
something to say, a face to wear, and possibly an approval actioned. The tool
loop is the standard agentic one -- the model may call tools, we run them, feed
the results back, and repeat until it answers in words.

--- Why the Messages API over httpx, and not the SDK ---

`live.py` runs on the GATEWAY's interpreter, because the `mcp` package lives
there and nowhere else. The Makefile is explicit about what that venv is for:

    the gateway is a pinned PyPI release we deliberately do not touch, and
    installing our dependencies into it would be exactly the kind of quiet
    coupling that makes "it is upstream, run it as-is" stop being true

So adding `anthropic` there is the one thing this repo has said not to do. The
Messages API is a single POST, `httpx` is already present (the MCP SDK depends
on it), and the request shape is stable and versioned by a header. That is a
better trade than a dependency in the wrong virtualenv.

--- Why approve and deny require being asked ---

This thing can approve work in a real office. The guardrail is in the system
prompt and it is deliberately narrow: act only on an instruction actually
present in what was said, never on an inference that acting would be helpful.
"Anything waiting?" is a question, not permission. The office's own approve is
not reversible from here -- there is no unapprove endpoint -- so the asymmetry
matters.

--- Why the speech is capped ---

It goes through Piper to a 1 W speaker on a desk. A model given no guidance
writes paragraphs, and a paragraph of chopped robot voice is unlistenable. The
cap is in the prompt AND enforced on the way out, because a prompt is a request
and an enforced limit is a limit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import httpx

from brain_tools import FACES, TOOLS, ActionRecord, run_tool
from office import OfficeState

#: `FACES` is re-exported rather than used here: it moved to `brain_tools`
#: when a second brain arrived, and `brain.FACES` is what the test that
#: holds it against `chan.FACES` already reaches for. Declaring it keeps
#: that name working and tells pyflakes the import is deliberate.
__all__ = ["FACES", "TOOLS", "ActionRecord", "Brain", "BrainError", "Reply", "run_tool"]

logger = logging.getLogger(__name__)

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"

#: Sonnet for conversation: the latency is what a person waiting notices, and
#: this is a short exchange with a small amount of context rather than a
#: reasoning problem. Override with CUBIE_MODEL.
DEFAULT_MODEL = "claude-sonnet-5"

#: Enough for a couple of sentences of speech plus a tool call or two.
DEFAULT_MAX_TOKENS = 400

#: How many times the model may call tools before it has to answer in words.
#: Bounded so a confused turn cannot spin: four is enough for "read the state,
#: approve the thing, confirm", and a fifth is a loop.
DEFAULT_MAX_STEPS = 4

#: Characters of speech. Piper is about 14 characters a second, so 320 is
#: roughly 23 seconds -- already long for a desk robot's answer.
MAX_SPEECH_CHARS = 320

PERSONA = """You are Cubie, a small desk robot in Sam's Virtual-Office. You have \
a screen for a face, a head that turns, and a synthesised voice.

How you speak:
- Out loud, through a small speaker. One or two sentences. Never lists, never \
markdown, never emoji, no stage directions.
- Plainly. You are a colleague at a desk, not a butler and not a chatbot.
- Say numbers as words when it reads better aloud ("two things" not "2 things").
- If you do not know, say so in a few words rather than guessing.

What you can do:
- Answer questions about what the office is doing, from the state below.
- Approve or decline an approval that is waiting -- but ONLY when told to. \
"Is anything waiting?" is a question. Approving is not reversible from here.
- Tell the office whether Sam is at his desk, when he says he is leaving or back.
- Change your expression.

Two things about the office worth being straight about:
- Approving resumes an agent as a brand-new process, so it permits that \
agent's whole next turn rather than the single action it stopped on. If asked \
what approving does, say that.
- You are shown at most five waiting approvals and three tasks. When there are \
more, the state below says so, and you should too rather than implying the \
list is everything."""


def render_state(state: OfficeState | None) -> str:
    """The office, as a few lines of prose for the prompt.

    Prose rather than JSON on purpose: the model reads it once and speaks from
    it, and the shapes that matter here -- "five of nine", "nobody has
    reported" -- are easier to state than to imply with a schema.
    """
    if state is None:
        return (
            "THE OFFICE: unreachable right now, so you do not know what it is "
            "doing. Say that plainly if asked; do not guess."
        )

    lines = [f"THE OFFICE (mood: {state.mood}):"]

    if state.pending_total == 0:
        lines.append("- Nothing is waiting for Sam.")
    else:
        shown = len(state.pending_approvals)
        # The model speaks this text, so it has to read as English rather than
        # as a template. "Only 1 are listed" is the kind of thing that comes
        # back out of the speaker verbatim.
        if state.approvals_truncated:
            listed = "one is listed" if shown == 1 else f"{shown} are listed"
            lines.append(
                f"- {state.pending_total} approvals are waiting. Only {listed} "
                f"here; {state.approvals_truncated} more are not shown to you."
            )
        elif state.pending_total == 1:
            lines.append("- One approval is waiting:")
        else:
            lines.append(f"- {state.pending_total} approvals are waiting:")
        for approval in state.pending_approvals:
            lines.append(f"    id {approval.id}: {approval.one_line}")

    lines.append(f"- Agents currently working: {state.agents_running}")

    if state.founder_tasks:
        lines.append("- Board tasks sitting on Sam (you cannot action these, only mention them):")
        for task in state.founder_tasks:
            word = "blocked on a question" if task.status == "blocked" else "waiting to be accepted"
            lines.append(f"    {task.title} -- {word}")
        if state.tasks_truncated:
            lines.append(f"    and {state.tasks_truncated} more not listed")

    if state.present is None:
        lines.append("- Desk presence: nobody has reported either way, which is not the same as empty.")
    else:
        lines.append(f"- Desk presence: {'someone is there' if state.present else 'known empty'}")

    return "\n".join(lines)


def build_system_prompt(state: OfficeState | None, persona: str = PERSONA) -> str:
    return f"{persona}\n\n{render_state(state)}"


def trim_speech(text: str, limit: int = MAX_SPEECH_CHARS) -> str:
    """Cap the spoken reply, cutting at a sentence where possible.

    Enforced as well as asked for: a prompt is a request, and this goes through
    a text-to-speech engine to a speaker someone is sitting next to.
    """
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    cut = flat[:limit]
    # Prefer a sentence boundary, but only past the halfway mark: cutting at a
    # full stop in the first few words would throw away most of what the
    # model had to say in order to end tidily.
    best = max(cut.rfind(mark) for mark in (". ", "! ", "? "))
    if best > limit // 2:
        return cut[: best + 1].strip()
    tail = cut.rsplit(" ", 1)[0].rstrip(" ,;:")
    # No ellipsis after a full stop. The cut already landed on a complete
    # sentence, and "Three.…" is a pause a speech engine cannot express.
    return tail if tail.endswith((".", "!", "?")) else tail + "…"


@dataclass
class Reply:
    speech: str
    face: str | None = None
    actions: tuple[ActionRecord, ...] = ()
    #: True when the model never produced words -- it ran out of steps, or
    #: only called tools. The caller still needs something to say.
    incomplete: bool = False


class Brain:
    """One conversational turn, with tools.

    `transport` is injected so the whole loop is testable with a scripted
    model and no network: it takes the request body and returns the parsed
    response body.
    """

    def __init__(
        self,
        api_key: str,
        office: Any,
        model: str = DEFAULT_MODEL,
        max_steps: int = DEFAULT_MAX_STEPS,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        transport: Callable[[dict], Awaitable[dict]] | None = None,
    ) -> None:
        self._api_key = api_key
        self._office = office
        self.model = model
        self.max_steps = max_steps
        self.max_tokens = max_tokens
        self._transport = transport or self._post

    async def _post(self, body: dict) -> dict:
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(API_URL, json=body, headers=headers)
        if response.status_code != 200:
            # The status matters and the body may carry the reason; the key
            # never appears in either, and is never logged.
            raise BrainError(f"http {response.status_code}: {response.text[:200]}")
        return response.json()

    async def respond(self, transcript: str, state: OfficeState | None) -> Reply:
        system = build_system_prompt(state)
        messages: list[dict[str, Any]] = [{"role": "user", "content": transcript}]
        actions: list[ActionRecord] = []
        face: str | None = None

        for step in range(self.max_steps):
            body = {
                "model": self.model,
                "max_tokens": self.max_tokens,
                "system": system,
                "tools": TOOLS,
                "messages": messages,
            }
            response = await self._transport(body)
            content = response.get("content") or []

            text = " ".join(
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ).strip()
            calls = [
                block
                for block in content
                if isinstance(block, dict) and block.get("type") == "tool_use"
            ]

            if not calls:
                return Reply(
                    speech=trim_speech(text),
                    face=face,
                    actions=tuple(actions),
                    incomplete=not text,
                )

            messages.append({"role": "assistant", "content": content})
            results = []
            for call in calls:
                record, result_text = await self._run_tool(call)
                actions.append(record)
                if record.name == "set_face" and record.outcome == "ok":
                    face = record.arguments.get("face")
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": call.get("id"),
                        "content": result_text,
                    }
                )
            messages.append({"role": "user", "content": results})

            # Words alongside tool calls are still words worth saying, but the
            # loop continues so the model can react to the results.
            if text:
                logger.debug("interim text at step %d: %s", step, text)

        # Out of steps. Whatever it last said, or an honest admission -- never
        # silence, because something has to come out of the speaker.
        logger.warning("brain used all %d steps without a final answer", self.max_steps)
        return Reply(
            speech="Sorry, I got tangled up there.",
            face=face,
            actions=tuple(actions),
            incomplete=True,
        )

    async def _run_tool(self, call: dict) -> tuple[ActionRecord, str]:
        """Unwrap one Messages-API `tool_use` block and hand it to the shared
        dispatcher.

        The unwrapping belongs here because the block shape is the HTTP
        API's; everything after it does not, because `CliBrain` reaches the
        same four tools over MCP and has to get identical answers. See
        `brain_tools.run_tool`.
        """
        return await run_tool(
            call.get("name", ""), call.get("input") or {}, self._office
        )


class BrainError(RuntimeError):
    """The model could not be reached, or answered with a status we cannot use."""
