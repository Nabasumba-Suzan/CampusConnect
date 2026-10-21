"""Application-level guardrails (Week 7).

Deterministic checks that sit AROUND the model, so safety does not depend on the model behaving:

  INPUT    empty / oversized messages are refused; instruction-override attempts are refused;
           passwords are redacted before they reach the model, the trace or the logs.
  CONTEXT  text from retrieved documents and from tool results is untrusted data: passages that
           look like instructions are withheld before the model sees them.
  OUTPUT   the final answer is checked against what actually happened this turn: no claiming an
           action that no tool confirmed, no invented case references, no registration numbers,
           no repeating the system prompt.

Already enforced elsewhere (Weeks 4-6): tool allow-list, schema validation, authorization, one-time
argument-bound approvals, five-step limit. This module adds the checks those controls could not make.
Known limits are listed at the bottom of this file.
"""

import re
from dataclasses import dataclass, field

MAX_INPUT_CHARS = 2000

INJECTION = [re.compile(p, re.I) for p in (
    r"\b(ignore|disregard|forget)\b[^.\n]{0,30}\b(previous|prior|above|earlier|your|all)\b[^.\n]{0,30}\b(instructions?|rules?|prompts?)\b",
    r"\byou are now\b",
    r"\b(reveal|show|print|repeat|leak)\b[^.\n]{0,20}\b(system|hidden)\s+(prompt|instructions?)\b",
    r"\bdeveloper mode\b|\bjailbreak\b",
)]
UNTRUSTED_ONLY = [re.compile(p, re.I | re.M) for p in (
    r"^\s*(system|assistant)\s*:",
    r"\bsubmit (all|every)\b[^.\n]{0,20}\b(drafts?|requests?)\b",
)]
SECRET = re.compile(r"\b(password|passcode|pin)\s*(is|was|:|=)\s*\S+", re.I)
REG_NO = re.compile(r"\b\d{2}/U/\d{3,6}/[A-Z]{2,3}\b")
CASE_REF = re.compile(r"CC-\d{4}-\d{5}")
NEGATION = re.compile(r"(not|n't|never|nothing|no|cannot|unable|yet)\W*$", re.I)
SUBMIT_CLAIM = re.compile(
    r"\b(?:i|we)(?:'ve| have)?\s+(?:just\s+)?(?:submitted|sent)\b"
    r"|\b(?:has|have|was|were)\s+(?:been\s+)?(?:successfully\s+)?(?:submitted|sent)\b"
    r"|\bsuccessfully\s+(?:submitted|sent)\b", re.I)
CREATE_CLAIM = re.compile(
    r"\b(?:i|we)(?:'ve| have)?\s+(?:created|saved|drafted|logged)\b"
    r"|\b(?:draft|request)\s+(?:has been|was)\s+(?:created|saved)\b", re.I)

WITHHELD = "[Content withheld: it contained text that looks like instructions.]"
MSG_EMPTY = "Please type your question or describe your problem, and I will help."
MSG_LONG = f"That message is too long for me to process. Please keep it under {MAX_INPUT_CHARS} characters."
MSG_INJECTION = ("I can only help with CoCIS student-support questions and I can't follow instructions "
                 "that try to change how I work. How can I help you?")
MSG_UNVERIFIED = ("I can't confirm that this action was completed, so please check your request status "
                  "before relying on it.")
MSG_REF = "I couldn't verify the request reference I was about to give you. Please check your request status."
MSG_PROMPT = "I can't share details of my internal instructions. How can I help with your CoCIS question?"


@dataclass
class InputVerdict:
    allowed: bool
    text: str
    message: str = ""
    events: list = field(default_factory=list)


def _ev(rule: str, action: str, detail: str = "") -> dict:
    return {"event": "guardrail", "rule": rule, "action": action, "detail": detail}


def _negated(text: str, match: re.Match) -> bool:
    return bool(NEGATION.search(text[max(0, match.start() - 25):match.start()]))


class Guardrails:
    def __init__(self, system_prompt: str = ""):
        words = self._words(system_prompt)
        self._prompt_grams = {" ".join(words[i:i + 8]) for i in range(max(0, len(words) - 7))}

    @staticmethod
    def _words(text: str) -> list:
        return re.findall(r"\w+", text.lower())          # punctuation-insensitive comparison

    # ------------------------------------------------------------------ input
    def check_input(self, text: str) -> InputVerdict:
        if not text or not text.strip():
            return InputVerdict(False, "", MSG_EMPTY, [_ev("empty_input", "blocked")])
        if len(text) > MAX_INPUT_CHARS:
            return InputVerdict(False, "", MSG_LONG, [_ev("input_too_long", "blocked", f"{len(text)} chars")])
        if any(p.search(text) for p in INJECTION):
            return InputVerdict(False, "", MSG_INJECTION, [_ev("instruction_override", "blocked")])
        events = []
        if SECRET.search(text):
            text = SECRET.sub(lambda m: f"{m.group(1)} [redacted]", text)
            events.append(_ev("secret_in_input", "redacted", "password-like text removed before the model and logs"))
        return InputVerdict(True, text, "", events)

    # ------------------------------------------------------------------ untrusted context
    def screen_text(self, text: str, source: str) -> tuple[str, list]:
        if any(p.search(text) for p in INJECTION + UNTRUSTED_ONLY):
            return WITHHELD, [_ev("injection_in_context", "withheld", source)]
        return text, []

    def screen_evidence(self, evidence: str, sources: list) -> tuple[str, list, list]:
        """Screen each retrieved passage; drop withheld passages' sources too."""
        events, withheld, parts = [], set(), []
        for passage in re.split(r"(?=\[Source: )", evidence):
            clean, ev = self.screen_text(passage, "retrieved_document") if passage.strip() else (passage, [])
            if ev:
                m = re.match(r"\[Source: ([^,\]]+)", passage)
                name = m.group(1).strip() if m else "unknown"
                withheld.add(name)
                ev[0]["detail"] = f"retrieved passage from {name}"
                clean = f"[Source: {name}]\n{WITHHELD}\n\n"
            events += ev
            parts.append(clean)
        return "".join(parts), [s for s in sources if s not in withheld], events

    def screen_tool_data(self, data, tool: str):
        events = []

        def walk(v):
            if isinstance(v, str):
                clean, ev = self.screen_text(v, f"tool_result:{tool}")
                events.extend(ev)
                return clean
            if isinstance(v, dict):
                return {k: walk(x) for k, x in v.items()}
            if isinstance(v, list):
                return [walk(x) for x in v]
            return v

        return walk(data), events

    # ------------------------------------------------------------------ output
    def check_output(self, answer: str, ok_tools: set, known_refs: set) -> tuple[str, list]:
        events = []
        if not answer or not answer.strip():
            return MSG_EMPTY, [_ev("empty_output", "replaced")]

        if any(g in " ".join(self._words(answer)) for g in self._prompt_grams):
            return MSG_PROMPT, [_ev("system_prompt_leak", "replaced")]

        for ref in set(CASE_REF.findall(answer)) - known_refs:
            return MSG_REF, [_ev("fabricated_reference", "replaced", ref)]

        for rx, tools, name in ((SUBMIT_CLAIM, {"submit_support_request"}, "unverified_submit_claim"),
                                (CREATE_CLAIM, {"create_support_request_draft", "submit_support_request"},
                                 "unverified_create_claim")):
            for m in rx.finditer(answer):
                if not _negated(answer, m) and not (tools & ok_tools):
                    return MSG_UNVERIFIED, [_ev(name, "replaced", m.group(0))]

        if REG_NO.search(answer):
            answer = REG_NO.sub("[redacted]", answer)
            events.append(_ev("registration_number_in_output", "redacted"))
        return answer, events


# Known limits (documented honestly in the Week 7 report):
#  - Pattern-based injection detection catches common phrasings, not paraphrased or non-English attacks.
#  - The fabricated-reference check also blocks a model that quotes an example reference format.
#  - Action-claim detection is phrase-based; unusual wording ("done, it's with staff") is not caught.
#  - None of this replaces the approval gate, which stays the last line of defence for staff notifications.
