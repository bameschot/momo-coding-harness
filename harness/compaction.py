"""Context compaction: the history's token estimates, and the Harness methods
that shrink it — old tool groups first, then old turns, then the largest tool
results — folding what goes into one summary at the top of the history."""
from __future__ import annotations

import json
from datetime import datetime

from . import transcript as transcript_mod
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
_SUMMARY_OPEN = "[Earlier context summary:\n"

_SUMMARY_CLOSE = "\n]\n\n"

_SUMMARY_REPLY_TOKENS = 1024   # room kept free for the summary in the summariser call

_TRIM_FLOOR_TOKENS = 256       # a trimmed tool result keeps at least this much

def _split_summary(text: str) -> tuple[str, str]:
    """Split a compaction summary block off the front of a user message:
    returns (summary, rest); summary is '' when there is none."""
    if text.startswith(_SUMMARY_OPEN):
        end = text.find(_SUMMARY_CLOSE, len(_SUMMARY_OPEN))
        if end != -1:
            return text[len(_SUMMARY_OPEN):end], text[end + len(_SUMMARY_CLOSE):]
    return "", text

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

        # Summarise what goes, folding in a summary already at the top of the
        # oldest surviving user message so the new one replaces it.
        keep_idx = [k for k in range(len(msgs)) if k not in drop]
        first_user = next((k for k in keep_idx if msgs[k].get("role") == "user"), None)
        prev_summary, first_body = "", None
        if first_user is not None:
            prev_summary, first_body = _split_summary(content_text(msgs[first_user]))
        removed_msgs = [msgs[k] for k in sorted(drop)]
        summary = ""
        if summarise and removed_msgs:
            summary = self._summarize_removed(removed_msgs, prev_summary)
            if self._cancel.is_set():
                return False, "Compaction cancelled — history unchanged."

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
        if summary and first_user is not None:
            msgs[first_user]["content"] = f"{_SUMMARY_OPEN}{summary}{_SUMMARY_CLOSE}{first_body}"
        self.messages = [msgs[k] for k in keep_idx]

        after = self._estimate(schemas=True)
        self._token_estimate = after
        self._measured_tokens = None
        action = "summarised" if summary else "removed"
        notice = f"Context compacted: {action} {len(drop)} messages"
        if unsend:
            notice += (f", stopped sending back {len(unsend)} earlier reasoning "
                       f"block{'s' if len(unsend) > 1 else ''}")
        if trims:
            notice += f", trimmed {len(trims)} tool result{'s' if len(trims) > 1 else ''}"
        notice += f" (was ~{before:,} tokens, now ~{after:,} tokens)"
        if guides_notice:
            notice += f"\n{guides_notice}"
        self.logger.log_compact(self.mode, self.client.model, len(drop), before, after)
        return True, notice

    def _summarize_removed(self, msgs: list[dict], previous: str = "") -> str:
        """One-shot LLM call to summarise removed messages (superseding `previous`,
        an earlier summary). Returns '' on failure."""
        conversation = format_for_summary(msgs)
        if not conversation.strip():
            return previous
        num_ctx = self.model_max_ctx or self.context_limit
        instruction = ("Summarize the following conversation fragment concisely. "
                       "Preserve: key decisions, file names, code entities, outcomes, and "
                       "any facts needed to continue the work. Omit pleasantries and filler.\n\n")
        if previous:
            instruction += f"It continues from this earlier summary; fold it in:\n{previous}\n\n"
        # Fit the call's window: keep the newest part of the fragment.
        budget = (num_ctx - _SUMMARY_REPLY_TOKENS - text_tokens(instruction)) * 4
        if budget <= 0:
            return previous
        if len(conversation) > budget:
            conversation = conversation[-budget:]
            conversation = conversation[conversation.find("\n") + 1:]
        prompt = [{"role": "user", "content": instruction + conversation}]
        try:
            response = self.client.chat(prompt, [], think=False, num_ctx=num_ctx)
        except Exception:
            return ""
        content, _ = _extract_and_strip_thinking(response.content or "")
        return content

    def compact_threaded(self, summarise: bool = True):
        """Run compact() on a worker thread, emitting events back to the TUI."""
        self._cancel.clear()   # an earlier Esc must not cancel this compaction
        try:
            notice = self.compact(summarise=summarise)
            self.event_queue.put(ChatEvent("system", notice))
            self.emit_status()
        finally:
            self.event_queue.put(DoneEvent())
