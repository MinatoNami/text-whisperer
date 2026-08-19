"""Summarisation via a local OpenAI-compatible server (LM Studio, Ollama, …).

Nothing leaves the machine: the default endpoint is loopback, matching the
rest of the pipeline. Long transcripts are folded in map-reduce fashion so an
hour-long meeting does not have to fit in one context window.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

# Reasoning models wrap their scratchpad in these; it is not the summary.
_THINK = re.compile(r"<(think|thinking|reasoning)>.*?</\1>", re.DOTALL | re.IGNORECASE)

SYSTEM = (
    "You summarise meeting transcripts. The transcript comes from automatic "
    "speech recognition, so expect mis-heard words and no speaker labels. "
    "Be concise and factual. Never invent details that are not in the text."
)

SUMMARY_PROMPT = """Summarise this meeting transcript in Markdown, using exactly these sections:

## Summary
Two or three sentences on what the meeting was about.

## Key points
- The substantive points discussed.

## Decisions
- Decisions actually reached. Write "None recorded." if there were none.

## Action items
- Who is doing what, if stated. Write "None recorded." if there were none.

Transcript:
---
{text}
---"""

CHUNK_PROMPT = """This is part {n} of {total} of a longer meeting transcript.
List the substantive points, decisions and action items in this part as terse
bullets. No preamble.

Transcript part:
---
{text}
---"""

REDUCE_PROMPT = """These are notes taken from consecutive parts of one meeting.
Merge them into a single summary in Markdown, using exactly these sections:

## Summary
Two or three sentences on what the meeting was about.

## Key points
- The substantive points, deduplicated across parts.

## Decisions
- Decisions actually reached. Write "None recorded." if there were none.

## Action items
- Who is doing what, if stated. Write "None recorded." if there were none.

Notes:
---
{text}
---"""


class LLMError(RuntimeError):
    pass


@dataclass(frozen=True)
class LLMConfig:
    base_url: str = "http://127.0.0.1:1234"
    model: str = ""
    timeout: float = 600.0
    # Rough character budget per request. Deliberately conservative: the point
    # is to stay well inside whatever context the loaded model has.
    chunk_chars: int = 12000


class LLMClient:
    def __init__(self, config: LLMConfig):
        self.config = config
        self._http = httpx.Client(timeout=httpx.Timeout(config.timeout, connect=10.0))

    def close(self) -> None:
        self._http.close()

    @property
    def _chat_url(self) -> str:
        return f"{self.config.base_url.rstrip('/')}/v1/chat/completions"

    def available_model(self) -> str:
        """Whatever the server has loaded, unless one was configured."""
        if self.config.model:
            return self.config.model
        try:
            response = self._http.get(
                f"{self.config.base_url.rstrip('/')}/v1/models", timeout=10.0
            )
            response.raise_for_status()
            models = response.json().get("data") or []
        except (httpx.HTTPError, ValueError) as exc:
            raise LLMError(
                f"no LLM reachable at {self.config.base_url} ({exc}). "
                "Is LM Studio running with a model loaded?"
            ) from exc
        if not models:
            raise LLMError(f"{self.config.base_url} has no model loaded")
        for model in models:
            model_id = str(model.get("id") or "").strip()
            if model_id:
                return model_id
        raise LLMError(f"{self.config.base_url} returned no usable model id")

    def _complete(self, prompt: str, model: str) -> str:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "stream": False,
        }
        try:
            response = self._http.post(self._chat_url, json=payload)
        except httpx.HTTPError as exc:
            raise LLMError(f"request to {self._chat_url} failed: {exc}") from exc
        if response.status_code >= 400:
            raise LLMError(f"LLM returned {response.status_code}: {response.text[:300]}")
        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError) as exc:
            raise LLMError(f"unexpected LLM response: {response.text[:300]}") from exc
        return strip_reasoning(content)

    def summarise(self, text: str, on_progress=None) -> str:
        """Summarise a transcript, folding it in parts if it is long."""
        text = (text or "").strip()
        if not text:
            raise LLMError("there is nothing to summarise")

        model = self.available_model()
        chunks = split_for_context(text, self.config.chunk_chars)

        if len(chunks) == 1:
            if on_progress:
                on_progress(0.1, "summarising")
            summary = self._complete(SUMMARY_PROMPT.format(text=chunks[0]), model)
            if on_progress:
                on_progress(1.0, "done")
            return _require_content(summary)

        # map: notes per part, then reduce them into one summary
        notes = []
        for index, chunk in enumerate(chunks, start=1):
            if on_progress:
                on_progress(index / (len(chunks) + 1), f"part {index} of {len(chunks)}")
            notes.append(
                self._complete(
                    CHUNK_PROMPT.format(n=index, total=len(chunks), text=chunk), model
                )
            )
        if on_progress:
            on_progress(len(chunks) / (len(chunks) + 1), "merging")
        summary = self._complete(REDUCE_PROMPT.format(text="\n\n".join(notes)), model)
        if on_progress:
            on_progress(1.0, "done")
        return _require_content(summary)


def _require_content(summary: str) -> str:
    if not summary.strip():
        raise LLMError(
            "the model returned only reasoning and no summary — it may have hit "
            "its token limit; try a smaller model or a shorter transcript"
        )
    return summary


def strip_reasoning(text: str) -> str:
    """Remove <think> blocks that reasoning models emit before their answer."""
    cleaned = _THINK.sub("", text or "")
    # An unterminated block means the model was cut off mid-thought, so
    # everything from that tag onward is scratchpad, not an answer. Dropping it
    # can leave nothing, which the caller reports as a failed summary rather
    # than passing reasoning off as a summary.
    opening = re.search(r"<(?:think|thinking|reasoning)>", cleaned, re.IGNORECASE)
    if opening:
        cleaned = cleaned[: opening.start()]
    return cleaned.strip()


def split_for_context(text: str, limit: int) -> list[str]:
    """Split on line boundaries so timestamped lines are never cut in half."""
    if len(text) <= limit:
        return [text]
    chunks, current = [], ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > limit and current:
            chunks.append(current)
            current = ""
        # A single line longer than the limit is pathological; hard-split it.
        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]
        current += line
    if current.strip():
        chunks.append(current)
    return chunks
