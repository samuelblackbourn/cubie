"""The four tools the brain has, and the one place they are carried out.

Extracted from `brain.py` when a second brain arrived. `Brain` calls the model
over HTTP and dispatches tool calls itself; `CliBrain` hands the same four
tools to a headless `claude` through an MCP server that runs in a different
process (`office_mcp.py`). Two brains, one tool surface -- and the surface has
to be one implementation, not two, because what is subtle here is not the
plumbing but the OUTCOMES.

--- Why the outcome text is the valuable part ---

`approve_request` can fail in three ways that mean completely different things
to the person standing in front of him:

  * STALE -- somebody already handled it. The office state he was given is out
    of date. He must say so and NOT retry, because a retry is a second grant.
  * REJECTED -- the office refused the shape of the request. That is our bug,
    and retrying it will fail identically.
  * UNREACHABLE -- nothing happened at all, so the work is still waiting.

A second, hand-written copy of that mapping would drift, and the drift would be
invisible: the model would be told "done" for something that never happened, or
would retry a grant that already took effect. So `run_tool` is the only thing
in this repo that turns an `Outcome` into words for the model, and both brains
go through it.

--- Nothing here talks to the robot ---

`set_face` does not move the face. It validates the name and records the
choice; the caller applies it. That is the same split `tracking.py` and
`idle.py` already make, and it is what lets the MCP server -- which has no
`chan`, no servos and no display -- carry the tool at all: it reports the
choice back and the character stack does the work.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from office import Outcome

#: The faces the board actually has, in the firmware's own order.
#:
#: Duplicated from the firmware's `kFaceNames` rather than derived, because the
#: character stack must not import the board to know a list of six strings --
#: and a test asserts the two agree. The next face to be added is `angry`
#: (PERSONALITY.md, Tier 1): add it in one place and the model can name a face
#: the board rejects, or the board grows a face the model is never told about.
FACES = ("idle", "happy", "thinking", "sad", "surprised", "embarrassed")


#: Outcomes rendered for the model, so it can tell the person the truth about
#: what happened rather than assuming success.
OUTCOME_TEXT = {
    Outcome.OK: "done",
    Outcome.STALE: (
        "that one was already handled by someone else -- the office state you "
        "were given is out of date. Say so; do not retry."
    ),
    Outcome.REJECTED: "the office refused the request as malformed. Do not retry it.",
    Outcome.UNREACHABLE: "could not reach the office, so nothing happened.",
}


@dataclass(frozen=True)
class ActionRecord:
    """One tool the model called, and how it went. For the log and the tests."""

    name: str
    arguments: dict[str, Any]
    outcome: str
    detail: str = ""


class OfficeActions(Protocol):
    """The three office calls a tool can make.

    A Protocol rather than the concrete `OfficeClient` so the MCP server, the
    tests and `Brain` can each supply their own -- and so this module does not
    drag an HTTP client into a process that may not need one.
    """

    async def approve(self, approval_id: str) -> tuple[Outcome, str]: ...

    async def deny(
        self, approval_id: str, reason: str | None = None
    ) -> tuple[Outcome, str]: ...

    async def set_presence(self, present: bool) -> tuple[Outcome, str]: ...


TOOLS: list[dict[str, Any]] = [
    {
        "name": "approve_request",
        "description": (
            "Approve one pending approval, letting that agent carry on. "
            "IMPORTANT: this resumes the agent by starting a brand-new "
            "process, so the grant covers its whole resumed turn, not just "
            "the one action it paused on. It cannot be undone from here. "
            "Only call this when the person has actually told you to approve "
            "something."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "The approval's id, exactly as given in the office state.",
                }
            },
            "required": ["id"],
        },
    },
    {
        "name": "deny_request",
        "description": (
            "Decline one pending approval, optionally saying why. The reason "
            "is passed to the agent. Only call this when the person has "
            "actually told you to decline something."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "The approval's id, exactly as given in the office state.",
                },
                "reason": {
                    "type": "string",
                    "description": "Short explanation for the agent. Optional.",
                },
            },
            "required": ["id"],
        },
    },
    {
        "name": "set_presence",
        "description": (
            "Tell the office whether someone is at the desk. Use it when the "
            "person says they are leaving or back. A boolean only -- the "
            "office deliberately accepts nothing else."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"present": {"type": "boolean"}},
            "required": ["present"],
        },
    },
    {
        "name": "set_face",
        "description": (
            "Change your expression while you answer. Use it when the "
            "expression carries something the words do not -- looking "
            "thinking while you work something out, sad when the news is bad. "
            "Not needed for every reply."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"face": {"type": "string", "enum": list(FACES)}},
            "required": ["face"],
        },
    },
]


async def run_tool(
    name: str, arguments: Any, office: OfficeActions
) -> tuple[ActionRecord, str]:
    """Carry out one tool call. Returns what to log, and what to tell the model.

    Argument validation is deliberately defensive rather than schema-trusting:
    `strict` is not set on these tools, an MCP client may hand us anything, and
    a malformed call has to come back as words the model can act on -- never as
    an exception that kills the turn and leaves the speaker silent.
    """
    if not isinstance(arguments, dict):
        return (
            ActionRecord(name, {}, "rejected", "arguments were not an object"),
            "that call was malformed",
        )

    if name == "set_face":
        wanted = arguments.get("face")
        if wanted not in FACES:
            return (
                ActionRecord(name, arguments, "rejected", f"unknown face {wanted!r}"),
                f"{wanted!r} is not one of your faces",
            )
        return ActionRecord(name, arguments, "ok"), "done"

    if name == "approve_request":
        ident = arguments.get("id")
        if not isinstance(ident, str) or not ident.strip():
            return (
                ActionRecord(name, arguments, "rejected", "no id"),
                "you did not give an id",
            )
        outcome, detail = await office.approve(ident.strip())
        return ActionRecord(name, arguments, outcome.value, detail), OUTCOME_TEXT[outcome]

    if name == "deny_request":
        ident = arguments.get("id")
        if not isinstance(ident, str) or not ident.strip():
            return (
                ActionRecord(name, arguments, "rejected", "no id"),
                "you did not give an id",
            )
        reason = arguments.get("reason")
        outcome, detail = await office.deny(
            ident.strip(), reason if isinstance(reason, str) else None
        )
        return ActionRecord(name, arguments, outcome.value, detail), OUTCOME_TEXT[outcome]

    if name == "set_presence":
        present = arguments.get("present")
        if not isinstance(present, bool):
            return (
                ActionRecord(name, arguments, "rejected", "present was not a boolean"),
                "presence has to be true or false",
            )
        outcome, detail = await office.set_presence(present)
        return ActionRecord(name, arguments, outcome.value, detail), OUTCOME_TEXT[outcome]

    return (
        ActionRecord(name, arguments, "rejected", "unknown tool"),
        f"{name} is not a tool you have",
    )
