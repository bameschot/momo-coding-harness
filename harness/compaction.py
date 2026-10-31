"""Context compaction: the history's token estimates, and the Harness methods
that shrink it — old tool groups first, then old turns, then the largest tool
results — folding what goes into one summary at the top of the history."""
from __future__ import annotations

import json
import re
from datetime import datetime

from . import transcript as transcript_mod
from . import todos as todos_mod
from .events import ChatEvent, DoneEvent
from .toolcall_text import _extract_and_strip_thinking


def content_text(m: dict) -> str:
    content = m.get("content") or ""
    if isinstance(content, list):
        content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
    return content

def text_tokens(text: str) -> int:
    return len(text) // 4

def calls_tokens(m: dict) -> int:
    calls = m.get("tool_calls") or []
    if not calls:
        return 0
    return text_tokens(json.dumps([tc.get("function", tc) for tc in calls], default=str))

def msg_tokens(m: dict, sent: set[int] | dict[int, str] = frozenset()) -> int:
    """Estimated tokens one message costs in a request.  A stored thinking
    message costs nothing unless its id is in `sent`: Harness._sent_thinking,
    or Harness._sent_reasoning, whose text (maybe trimmed) is what is counted."""
    if m.get("role") == "thinking":
        if id(m) not in sent:
            return 0
        return text_tokens(sent[id(m)] if isinstance(sent, dict) else content_text(m))
    return text_tokens(content_text(m)) + calls_tokens(m)

# Compaction folds the dropped history into this block at the top of the oldest
# remaining user message; a later compaction replaces it rather than stacking.
# The block is rendered from Harness.summary_state (see new_summary_state), not
# parsed back from text: each section has its own merge rule.
_SUMMARY_OPEN = "<context-summary>\n"

_SUMMARY_CLOSE = "\n</context-summary>\n\n"

# Sessions from before the structured summary: their free-text block is folded
# into the first structured one (as the "legacy" section until the LLM has).
_LEGACY_OPEN = "[Earlier context summary:\n"

_LEGACY_CLOSE = "\n]\n\n"

_SUMMARY_REPLY_TOKENS = 1536   # room kept free for the summary in the summariser call

_TRIM_FLOOR_TOKENS = 256       # a trimmed tool result keeps at least this much

_SUMMARY_MIN_TOKENS, _SUMMARY_MAX_TOKENS = 400, 2000   # rendered summary size cap

_MAX_REQUESTS = 8              # user requests kept verbatim: the first 2 and the newest
_REQUEST_CHARS = 300
_MAX_COMMANDS = 8
_COMMAND_CHARS = 160

# The LLM-written sections, in order: (key, heading, what goes under it).
_SECTIONS = (
    ("goal", "Goal",
     "One or two sentences: what the user wants done, and what done looks like."),
    ("constraints", "Constraints & preferences",
     "Requirements, prohibitions and preferences the user stated. Keep every one "
     "from the current summary unless the user revoked it."),
    ("progress", "Progress",
     "Three labelled lists — 'Done:', 'In progress:' (where the work stopped), "
     "'Blocked:'. Move finished items from In progress to Done."),
    ("decisions", "Decisions",
     "'- <decision>: <why>' for choices made, so they are not reopened."),
    ("failed", "Failed attempts / gotchas",
     "What was tried and did not work, and why, so it is not tried again."),
    ("next", "Next steps",
     "A numbered list of concrete steps (file paths, functions, commands). Drop "
     "steps that are done."),
)

# Heading text (lowercased) → section key, by the first word that tells them apart.
_HEADING_KEYS = (("goal", "goal"), ("constraint", "constraints"), ("preference", "constraints"),
                 ("progress", "progress"), ("decision", "decisions"), ("failed", "failed"),
                 ("gotcha", "failed"), ("next", "next"))

_RX_HEADING = re.compile(r"^\s*#{1,4}\s*\**\s*(.+?)\s*\**\s*:?\s*$")
_RX_EXIT = re.compile(r"exit code:? (-?\d+)")

# Tools whose `path` argument the summary lists, by what they do to it.
_READ_TOOLS = {"read_file", "read_symbol", "code_outline", "file_info"}
_WRITE_TOOLS = {"edit_file", "write_file", "append_to_file", "replace_symbol"}

def _split_summary(text: str) -> tuple[str, str]:
    """Split a compaction summary block (or a legacy free-text one) off the front
    of a user message: returns (summary, rest); summary is '' when there is none."""
    for opener, closer in ((_SUMMARY_OPEN, _SUMMARY_CLOSE), (_LEGACY_OPEN, _LEGACY_CLOSE)):
        if text.startswith(opener):
            end = text.find(closer, len(opener))
            if end != -1:
                return text[len(opener):end], text[end + len(closer):]
    return "", text

def _legacy_text(text: str) -> str:
    """The free-text summary of a session from before the structured one, if
    `text` starts with it."""
    return _split_summary(text)[0] if text.startswith(_LEGACY_OPEN) else ""

def new_summary_state() -> dict:
    """The summary's parts, kept on the Harness and saved with the session.
    requests/files/commands are filled by the harness from the dropped messages;
    sections by the summariser LLM, which updates the previous ones."""
    return {"n": 0, "requests": [], "requests_elided": 0, "sections": {},
            "files": {"modified": [], "moved": [], "deleted": [], "read": []},
            "commands": []}

def _clip(text: str, limit: int) -> str:
    """text cut to about `limit` chars, keeping its head and tail."""
    text = text.strip()
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    return f"{text[:head]} … {text[len(text) - (limit - head):]}"

def _call_args(tc: dict) -> tuple[str, dict]:
    fn = tc.get("function", tc)
    args = fn.get("arguments") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = {}
    return fn.get("name", "?"), args if isinstance(args, dict) else {}

def _paired_calls(messages: list[dict]):
    """(name, args, result) for each tool call in `messages`, its result matched by
    order (results follow their assistant message in call order); None when the
    result is not among them."""
    pending: list[tuple[str, dict]] = []
    for m in messages:
        role = m.get("role")
        if role == "assistant":
            for name, args in pending:
                yield name, args, None
            pending = [_call_args(tc) for tc in (m.get("tool_calls") or [])]
        elif role == "tool":
            if pending:
                name, args = pending.pop(0)
                yield name, args, content_text(m)
    for name, args in pending:
        yield name, args, None

def _add(lst: list, item: str) -> None:
    """Append item, moving it to the end when it is already there (newest last)."""
    if item in lst:
        lst.remove(item)
    lst.append(item)

def _discard(files: dict, path: str, *kinds: str) -> None:
    for kind in kinds:
        if path in files[kind]:
            files[kind].remove(path)

def _failing(status: str) -> bool:
    return status.startswith("error") or (status.startswith("exit") and status != "exit 0")

def merge_facts(state: dict, messages: list[dict]) -> dict:
    """A copy of `state` with what the harness can read off `messages` (being
    dropped) merged in: the user's requests, the files the tools touched, the
    commands run.  No LLM: these survive a failed or garbled summary."""
    st = json.loads(json.dumps(state))
    files, commands = st["files"], st["commands"]
    for m in messages:
        if m.get("role") == "user":
            _, body = _split_summary(content_text(m))
            if body.strip():
                st["requests"].append(_clip(" ".join(body.split()), _REQUEST_CHARS))
    if len(st["requests"]) > _MAX_REQUESTS:
        cut = len(st["requests"]) - _MAX_REQUESTS
        st["requests"] = st["requests"][:2] + st["requests"][2 + cut:]
        st["requests_elided"] += cut
    for name, args, result in _paired_calls(messages):
        failed = result is None or result.startswith("ERROR")
        if name == "run_command" and args.get("command"):
            cmd = _clip(" ".join(str(args["command"]).split()), _COMMAND_CHARS)
            error = (result or "")[len("ERROR:"):].strip()
            if result is None:
                status = "no result"
            elif result.startswith("ERROR"):
                status = f"error: {_clip(error.splitlines()[0], 80)}" if error else "error"
            elif "[running in the background" in result:
                status = "started in the background"
            else:
                codes = _RX_EXIT.findall(result)
                status = f"exit {codes[-1] if codes else 0}"
            prev = next((c for c in commands if c["cmd"] == cmd), None)
            failed_before = bool(prev and (prev["failed_before"] or _failing(prev["status"])))
            if prev:
                commands.remove(prev)
            commands.append({"cmd": cmd, "status": status,
                             "failed_before": failed_before and status == "exit 0"})
            continue
        if failed or (result or "").startswith("No change"):
            continue
        if name in _WRITE_TOOLS or name == "edit_files":
            paths = ([e.get("path") for e in args.get("edits") or [] if isinstance(e, dict)]
                     if name == "edit_files" else [args.get("path")])
            for p in filter(None, paths):
                _discard(files, p, "read", "deleted")
                _add(files["modified"], p)
        elif name == "delete_file" and args.get("path"):
            _discard(files, args["path"], "read", "modified")
            _add(files["deleted"], args["path"])
        elif name == "move_file" and args.get("src") and args.get("dst"):
            src, dst = args["src"], args["dst"]
            for kind in ("read", "modified"):
                if src in files[kind]:
                    files[kind].remove(src)
                    _add(files[kind], dst)
            _add(files["moved"], f"{src} → {dst}")
        elif name in _READ_TOOLS and args.get("path"):
            if args["path"] not in files["modified"]:
                _add(files["read"], args["path"])
    del commands[:-_MAX_COMMANDS]
    return st

def serialise_removed(messages: list[dict]) -> str:
    """The dropped messages as a labelled transcript for the summariser: tool-call
    arguments clipped (an edit's text is noise here), tool results kept head and
    tail (errors and test totals sit at the end), reasoning left out."""
    parts = []
    for m in messages:
        role = m.get("role", "")
        content = content_text(m)
        if role == "user":
            _, body = _split_summary(content)
            parts.append(f"[User]: {body}")
        elif role == "assistant":
            if content:
                parts.append(f"[Assistant]: {content}")
            for tc in (m.get("tool_calls") or []):
                name, args = _call_args(tc)
                shown = ", ".join(f"{k}={_clip(json.dumps(v, default=str), 200)}"
                                  for k, v in args.items())
                parts.append(f"[Assistant tool call]: {name}({shown})")
        elif role == "tool":
            name = m.get("name", "tool")
            limit = 2000 if content.startswith("ERROR") else 1800
            parts.append(f"[Tool result ({name})]: {_clip(content, limit)}")
    return "\n".join(parts)

def summary_prompt(state: dict, conversation: str) -> str:
    """The summariser instruction: update the LLM-written sections of `state`
    from `conversation` (the dropped messages, serialised)."""
    skeleton = "\n".join(f"## {heading}\n{what}" for _, heading, what in _SECTIONS)
    current = render_sections(state["sections"])
    requests = "\n".join(f"- {r}" for r in state["requests"]) or "none"
    return (
        "Older messages of a coding session are being removed from the model's context. "
        "You keep its working memory: update the summary below with what the removed "
        "messages add, so the work can continue without them. The harness itself keeps "
        "the user's requests, the files touched and the commands run, so do not list "
        "those.\n\n"
        "Reply with exactly these sections, in this order, with these headings. Under "
        "each, short '- ' bullets (at most 6), or the word none. No preamble.\n\n"
        f"{skeleton}\n\n"
        f"Current summary (keep what is still true):\n{current or 'none (this is the first summary)'}\n\n"
        f"The user's requests so far:\n{requests}\n\n"
        f"Removed messages:\n{conversation}")

def parse_sections(text: str) -> dict | None:
    """The sections of a summariser reply, by key ('' for a 'none' section), or
    None when it is not the asked-for shape (no Goal, or under 3 sections)."""
    found: dict[str, list[str]] = {}
    key = None
    for line in text.splitlines():
        if (h := _RX_HEADING.match(line)):
            name = re.sub(r"^(key|current|exact)\s+", "", h.group(1).lower())
            new = next((k for word, k in _HEADING_KEYS if name.startswith(word)), None)
            if new is not None:
                key = new
                found.setdefault(key, [])
                continue
            # Any other heading (a model's own "### Done" under Progress) is content.
        if key is not None:
            found[key].append(line.rstrip())
    if "goal" not in found or len(found) < 3:
        return None
    out = {}
    for k, lines in found.items():
        body = "\n".join(lines).strip()
        out[k] = "" if body.lower().strip("-*. ") in ("", "none", "n/a") else body
    return out

def render_sections(sections: dict, keep_bullets: int | None = None,
                    clip: int | None = None) -> str:
    out = []
    for key, heading, _ in _SECTIONS:
        body = sections.get(key, "")
        if not body:
            continue
        if keep_bullets is not None and key in ("progress", "decisions", "failed"):
            body = _keep_last_bullets(body, keep_bullets)
        if clip is not None and key != "next":
            body = _clip(body, clip)
        out.append(f"## {heading}\n{body}")
    if sections.get("legacy"):
        out.append(f"## Earlier notes\n{_clip(sections['legacy'], clip or 4000)}")
    return "\n\n".join(out)

def _keep_last_bullets(body: str, n: int) -> str:
    """body with only its last n bullets under each label (Done:, In progress:…)."""
    out: list[str] = []
    bullets: list[str] = []

    def flush():
        if len(bullets) > n:
            out.append(f"- ({len(bullets) - n} earlier)")
        out.extend(bullets[-n:])
        bullets.clear()
    for line in body.splitlines():
        if line.lstrip().startswith(("-", "*")) or line.lstrip()[:2].rstrip(".").isdigit():
            bullets.append(line)
        else:
            flush()
            out.append(line)
    flush()
    return "\n".join(out)

def _file_line(label: str, paths: list[str], keep: int | None) -> str:
    if keep is not None and len(paths) > keep:
        return f"{label}: {', '.join(paths[-keep:])} (+{len(paths) - keep} more)"
    return f"{label}: {', '.join(paths)}"

def render_summary(state: dict, budget: int, plan_line: str = "") -> str:
    """The summary block's body, shrunk step by step until it fits `budget`
    tokens: long file lists and old commands first, then old Done/Decisions
    bullets, then long sections.  Constraints and Next steps go last."""
    levels = (  # (read files kept, commands kept, request chars, bullets kept, section clip)
        (None, _MAX_COMMANDS, _REQUEST_CHARS, None, None),
        (5, 4, 150, None, None),
        (3, 3, 120, 3, None),
        (0, 2, 100, 1, 600),
        (0, 1, 80, 1, 300),
    )
    text = ""
    for read_keep, cmd_keep, req_chars, bullets, clip in levels:
        out = [f"Summary of earlier work in this conversation (compaction {state['n']}). "
               "Background, not a new request."]
        if state["requests"]:
            reqs = [f'{i}. "{_clip(r, req_chars)}"' for i, r in enumerate(state["requests"], 1)]
            if state["requests_elided"]:
                reqs.insert(2, f"   ({state['requests_elided']} more requests left out)")
            out.append("## User requests (verbatim, oldest first)\n" + "\n".join(reqs))
        sections = render_sections(state["sections"], bullets, clip)
        if sections:
            out.append(sections)
        files = state["files"]
        lines = [_file_line(kind, files[kind], read_keep if kind == "read" else
                            (None if read_keep is None else max(read_keep, 10)))
                 for kind in ("modified", "moved", "deleted", "read")
                 if files[kind] and not (kind == "read" and read_keep == 0)]
        if read_keep == 0 and files["read"]:
            lines.append(f"read: {len(files['read'])} files")
        if lines:
            out.append("## Files\n" + "\n".join(lines))
        cmds = state["commands"][-cmd_keep:]
        if cmds:
            out.append("## Commands (newest last)\n" + "\n".join(
                f"- `{c['cmd']}` → {c['status']}"
                + (" (failed earlier)" if c.get("failed_before") else "") for c in cmds))
        if plan_line:
            out.append(plan_line)
        text = "\n\n".join(out)
        if text_tokens(text) <= budget:
            break
    return text

def format_for_summary(messages: list[dict]) -> str:
    parts = []
    for m in messages:
        role = m.get("role", "")
        content = content_text(m)
        if role == "user":
            parts.append(f"User: {content}")
        elif role == "assistant":
            if content:
                parts.append(f"Assistant: {content}")
            for tc in (m.get("tool_calls") or []):
                fn = tc.get("function", {})
                parts.append(f"  [Tool call: {fn.get('name', '?')} {json.dumps(fn.get('arguments', {}))}]")
        elif role == "tool":
            name = m.get("name", "tool")
            snippet = content[:500] + ("…" if len(content) > 500 else "")
            parts.append(f"  [Tool result ({name}): {snippet}]")
    return "\n".join(parts)


class CompactionMixin:
    """Compaction for Harness: it works on the harness's messages, client,
    context limit and cancel flag."""

    def compact(self, summarise: bool = True) -> str:
        return self._compact(summarise)[1]

    def _compact(self, summarise: bool = True) -> tuple[bool, str]:
        """Shrink the history to a third of its budget (the limit minus the fixed
        system prompt).  Returns (changed, notice).

        Pass 1 drops old tool-call groups (assistant + its tool/thinking messages),
        pass 2 drops whole old turns, pass 3 trims the largest tool results that
        are left.  The running turn's question and its latest tool group are never
        dropped — deleting the result the model just asked for makes it ask again.
        Nothing is changed until the summary is in, so a cancel leaves it intact.
        """
        # Re-read the guides first, so the fixed size below includes any edits.
        old_guides = self._guides
        guides_notice = self.reload_guides() if self.guides else ""
        if self._guides == old_guides:
            guides_notice = ""
        msgs = self.messages
        before = self._token_estimate or self._estimate(schemas=True)
        sent = self._sent_reasoning()
        costs = [msg_tokens(m, sent) for m in msgs]
        fixed = costs[0] if msgs else 0
        if fixed >= self.context_limit:
            return False, (f"Context not compacted: the system prompt and tool reference alone are "
                           f"~{fixed:,} tokens, over the {self.context_limit:,}-token limit. "
                           "Raise it with /context <n> or /context <n>%.")
        target = fixed + (self.context_limit - fixed) // 3
        total = sum(costs)

        users = [i for i, m in enumerate(msgs) if m.get("role") == "user"]
        anchor = next((i for i, m in enumerate(msgs) if m is self._turn_user), None)
        if anchor is None:
            anchor = users[-1] if users else len(msgs)
        # The latest tool group of the running turn stays (it may be in flight).
        last_calls = next((i for i in range(len(msgs) - 1, anchor, -1)
                           if msgs[i].get("role") == "assistant" and msgs[i].get("tool_calls")), None)
        tail = last_calls if last_calls is not None else len(msgs)

        drop: set[int] = set()

        def take(idx):
            nonlocal total
            drop.add(idx)
            total -= costs[idx]

        # Pass 0: stop sending back the reasoning of earlier turns (/think-history
        # all) — the cheapest thing to lose; it stays in the transcript.
        unsend: list[int] = []
        for k in range(1, anchor):
            if total <= target:
                break
            if id(msgs[k]) in sent:
                unsend.append(k)
                total -= costs[k]
                costs[k] = 0       # a later pass dropping it saves nothing more

        # Pass 1: tool-call groups, oldest first.
        i = 1
        while i < tail and total > target:
            role = msgs[i].get("role")
            if role == "assistant":
                j = i + 1
                while j < tail and msgs[j].get("role") in ("tool", "thinking"):
                    j += 1
                if j > i + 1:
                    for k in range(i, j):
                        take(k)
                    i = j
                    continue
            elif role in ("tool", "thinking"):
                # A tool result with no assistant before it, or reasoning (it rides
                # on the assistant after it; dropping it only stops sending it back).
                take(i)
            i += 1

        # Pass 2: whole turns before the running one, oldest first, so what is
        # left still starts with a user message and keeps its tool pairing.
        starts = [u for u in users if u < anchor]
        if not starts or starts[0] > 1:
            starts.insert(0, 1)
        for n, s0 in enumerate(starts):
            if total <= target:
                break
            s1 = starts[n + 1] if n + 1 < len(starts) else anchor
            for k in range(s0, s1):
                if k not in drop:
                    take(k)

        # Pass 3: still over the target — trim the largest remaining tool results.
        # Trimming only to the limit would leave the context a few tokens under
        # it: every later request then sits at ~100% yet never re-triggers.
        trims: dict[int, str] = {}
        if total > target:
            for k in sorted((k for k in range(1, len(msgs))
                             if k not in drop and msgs[k].get("role") == "tool"),
                            key=lambda k: -costs[k]):
                excess = total - target
                if excess <= 0:
                    break
                keep = max(costs[k] - excess - 16, _TRIM_FLOOR_TOKENS)  # 16: the marker
                if keep >= costs[k]:
                    continue
                text = content_text(msgs[k])
                head, tail_chars = keep * 4 * 2 // 3, keep * 4 // 3
                cut = len(text) - head - tail_chars
                trims[k] = (f"{text[:head]}\n[… {cut:,} chars removed by context compaction …]\n"
                            f"{text[len(text) - tail_chars:]}")
                total -= costs[k] - text_tokens(trims[k])

        if not drop and not trims and not unsend:
            return False, (f"Context not compacted: nothing left to remove "
                           f"(~{total:,} of {self.context_limit:,} tokens).")

        # Summarise what goes.  The summary is rebuilt from self.summary_state
        # and replaces the block at the top of the oldest surviving user message.
        keep_idx = [k for k in range(len(msgs)) if k not in drop]
        first_user = next((k for k in keep_idx if msgs[k].get("role") == "user"), None)
        first_body = None
        if first_user is not None:
            _, first_body = _split_summary(content_text(msgs[first_user]))
        removed_msgs = [msgs[k] for k in sorted(drop)]
        state, summarised, unusable = self.summary_state, False, False
        if removed_msgs:
            state = merge_facts(self.summary_state, removed_msgs)
            state["n"] += 1
            if not state["sections"]:
                # A session from before the structured summary: keep its free text.
                carriers = ([msgs[first_user]] if first_user is not None else []) + removed_msgs
                legacy = next((t for m in carriers if m.get("role") == "user"
                               and (t := _legacy_text(content_text(m)))), "")
                if legacy:
                    state["sections"]["legacy"] = legacy
            if summarise:
                sections = self._summarize_removed(removed_msgs, state)
                if self._cancel.is_set():
                    return False, "Compaction cancelled — history unchanged."
                if sections is None:
                    unusable = True        # keep the previous sections as they were
                else:
                    summarised = True
                    if sections:
                        state["sections"] = {**state["sections"], **sections}
                        state["sections"].pop("legacy", None)

        # Commit.  Archive first: the trims below change the messages in place.
        self.compacted = transcript_mod.archive_compaction(
            self.compacted, msgs, drop, trims, keep_idx,
            max((r["n"] for r in self.compacted), default=0) + 1,
            datetime.now().isoformat(timespec="seconds"))
        # (In place: _turn_user tracks the running turn's message by identity.)
        for k, text in trims.items():
            msgs[k]["content"] = text
        for k in unsend:
            msgs[k]["unsent"] = True
        if removed_msgs and first_user is not None:
            budget = max(_SUMMARY_MIN_TOKENS,
                         min(_SUMMARY_MAX_TOKENS, (self.context_limit - fixed) // 10))
            body = render_summary(state, budget, "\n\n".join(
                filter(None, (self._plan_summary_line(), self._todos_summary_line()))))
            msgs[first_user]["content"] = f"{_SUMMARY_OPEN}{body}{_SUMMARY_CLOSE}{first_body}"
        self.summary_state = state
        self.messages = [msgs[k] for k in keep_idx]

        after = self._estimate(schemas=True)
        self._token_estimate = after
        self._measured_tokens = None
        action = "summarised" if summarised else "removed"
        notice = f"Context compacted: {action} {len(drop)} messages"
        if unsend:
            notice += (f", stopped sending back {len(unsend)} earlier reasoning "
                       f"block{'s' if len(unsend) > 1 else ''}")
        if trims:
            notice += f", trimmed {len(trims)} tool result{'s' if len(trims) > 1 else ''}"
        notice += f" (was ~{before:,} tokens, now ~{after:,} tokens)"
        if unusable:
            notice += ("\nThe summariser's reply was unusable: kept the previous summary, "
                       "with the files and commands updated.")
        if guides_notice:
            notice += f"\n{guides_notice}"
        self.logger.log_compact(self.mode, self.client.model, len(drop), before, after)
        return True, notice

    def _summarize_removed(self, msgs: list[dict], state: dict) -> dict | None:
        """One-shot LLM call that updates the LLM-written sections of `state` from
        the removed messages.  Returns the new sections ({} when the messages hold
        nothing to summarise), or None on failure or an unusable reply."""
        conversation = serialise_removed(msgs)
        if not conversation.strip():
            return {}
        num_ctx = self.model_max_ctx or self.context_limit
        # Fit the call's window: keep the newest part of the fragment (the user's
        # requests are in the prompt anyway, from the state).
        budget = (num_ctx - _SUMMARY_REPLY_TOKENS - text_tokens(summary_prompt(state, ""))) * 4
        if budget <= 0:
            return None
        if len(conversation) > budget:
            conversation = conversation[-budget:]
            conversation = conversation[conversation.find("\n") + 1:]
        prompt = [{"role": "user", "content": summary_prompt(state, conversation)}]
        try:
            response = self.client.chat(prompt, [], think=False, num_ctx=num_ctx)
        except Exception:
            return None
        content, _ = _extract_and_strip_thinking(response.content or "")
        return parse_sections(content)

    def _todos_summary_line(self) -> str:
        """The todos list while an item is open: compaction drops the old todos
        results, which were the only copy of it in the context."""
        return todos_mod.summary_line(getattr(self, "todos", []))

    def _plan_summary_line(self) -> str:
        """Where an executing plan stands, for the summary: the plan itself is in
        the system prompt, so only a pointer."""
        plan = getattr(self, "plan", None)
        if plan is None or getattr(self, "plan_phase", "") != "executing":
            return ""
        cur = plan.current_index()
        if cur is None:
            return ""
        return (f"Plan: step {cur + 1} of {len(plan.steps)} ({plan.steps[cur].title}) is "
                "current; the plan is in the system prompt.")

    def compact_threaded(self, summarise: bool = True):
        """Run compact() on a worker thread, emitting events back to the TUI."""
        self._cancel.clear()   # an earlier Esc must not cancel this compaction
        try:
            notice = self.compact(summarise=summarise)
            self.event_queue.put(ChatEvent("system", notice))
            self.emit_status()
        finally:
            self.event_queue.put(DoneEvent())
