# services/llm.py
import json
import re
from typing import List, Dict, Iterable

import anthropic

# ==============================
# Fixed model & lightweight settings
# ==============================
# Use a stable, publicly available model id.
MODEL_NAME = "claude-3-5-sonnet-20240620"
TEMPERATURE = 0.2

# Smaller caps for speed
TOKENS_FILTER = 500
TOKENS_SCORE  = 500

# Batch controls
BATCH_SIZE = 12          # # of posts per LLM call
MAX_TEXT_CHARS = 320     # truncate tweets for speed/stability

# ==============================
# Heuristics to avoid missing good posts
# ==============================
NUMERIC_REGEX = re.compile(
    r"(\d{1,3}(,\d{3})+|\d+)(\.\d+)?|%|\$|million|billion|thousand|per\s?cent|per\s?capita|\d{4}",
    re.IGNORECASE
)
CLAIMY_VERBS = re.compile(
    r"\b(said|says|claims?|stated|announced|mandates?|banned|requires?|raises?|cuts?|will|won't)\b",
    re.IGNORECASE
)

def looks_claimy(text: str) -> bool:
    if not text:
        return False
    t = text.strip()
    if len(t) < 30:
        return False
    if NUMERIC_REGEX.search(t) or CLAIMY_VERBS.search(t):
        return True
    if any(k in t.lower() for k in [
        "tax", "vote", "election", "redistrict", "crime", "unemployment", "inflation",
        "immigration", "vaccine", "border", "budget", "billion", "percent", "gun", "abortion"
    ]):
        return True
    return False

# ==============================
# Helpers
# ==============================
def _extract_text(resp) -> str:
    out = []
    try:
        for part in resp.content:
            if getattr(part, "type", None) == "text" and getattr(part, "text", None):
                out.append(part.text)
            elif isinstance(part, dict) and part.get("type") == "text":
                out.append(part.get("text", ""))
    except Exception:
        pass
    return "\n".join(out).strip()

def _anthropic_client(api_key: str) -> anthropic.Anthropic:
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is missing.")
    return anthropic.Anthropic(api_key=api_key)

def _chunks(seq: List[Dict], size: int) -> Iterable[List[Dict]]:
    for i in range(0, len(seq), size):
        yield seq[i:i+size]

def _trunc(s: str, n: int = MAX_TEXT_CHARS) -> str:
    s = (s or "").strip()
    return s[:n] + ("…" if len(s) > n else "")

def _escape_quotes(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')

def _call_json_array(client: anthropic.Anthropic, prompt: str, max_tokens: int) -> List[Dict]:
    """
    Calls Anthropic and returns a JSON array (or [] on hard failure).
    Surfaces useful error info if available.
    """
    try:
        resp = client.messages.create(
            model=MODEL_NAME,
            messages=[{"role": "user", "content": "Return ONLY valid minified JSON. No markdown, no code fences, no commentary.\n\n" + prompt}],
            max_tokens=max_tokens,
            temperature=TEMPERATURE,
        )
    except Exception as e:
        # Bubble up a concise message so Streamlit shows it
        raise RuntimeError(f"Anthropic request failed: {e!r}")

    text = _extract_text(resp).strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return [data]
        return data if isinstance(data, list) else []
    except Exception:
        cleaned = text.strip().strip("`").replace("\n", " ").replace("\r", " ")
        try:
            data = json.loads(cleaned)
            if isinstance(data, dict):
                return [data]
            return data if isinstance(data, list) else []
        except Exception:
            # Couldn’t parse; return empty so heuristics can still keep claimy posts
            return []

# ==============================
# Batch prompts
# ==============================
def _filter_prompt(items: List[Dict]) -> str:
    lines = []
    for x in items:
        lines.append(f'- id: {x.get("id","")}\n  text: "{_escape_quotes(_trunc(x.get("text","")))}"')
    posts_block = "\n".join(lines)
    return f"""
You are a PolitiFact intake editor. Decide for EACH post if it likely contains a fact-checkable claim per PolitiFact criteria:

1) Verifiable fact (not pure opinion/hyperbole)
2) Seems misleading or likely wrong
3) Significant (not trivial gotcha)
4) Likely to spread
5) A typical person would wonder: “Is that true?”

Be moderately lenient: if a post includes numbers, dates, measurable outcomes, or a specific policy/person assertion, lean **true**.

Return ONLY minified JSON array. One object per post with keys:
"id", "is_fact_checkable" (boolean), "reason" (short explanation citing criteria numbers).

Posts:
{posts_block}
""".strip()

def _score_prompt(items: List[Dict]) -> str:
    lines = []
    for x in items:
        lines.append(f'- id: {x.get("id","")}\n  text: "{_escape_quotes(_trunc(x.get("text","")))}"')
    posts_block = "\n".join(lines)
    return f"""
Score EACH post on two axes:
1) "checkable_score" (1-10): how definitively we can verify with evidence.
2) "importance_score" (1-10): public interest or potential harm if false.

Return ONLY minified JSON array. Object per post:
{{"id": "...", "checkable_score": integer, "importance_score": integer, "scoring_reason": "brief"}}

Posts:
{posts_block}
""".strip()

# ==============================
# Public API
# ==============================
def filter_posts_with_llm(posts: List[Dict], *, anthropic_api_key: str) -> List[Dict]:
    """
    Batch-filter posts. Also keeps obviously claim-y posts if LLM declines or errors.
    """
    if not posts:
        return []
    client = _anthropic_client(anthropic_api_key)

    kept: List[Dict] = []
    for batch in _chunks(posts, BATCH_SIZE):
        llm_items = [{"id": p.get("id",""), "text": p.get("text","")} for p in batch]
        results = _call_json_array(client, _filter_prompt(llm_items), max_tokens=TOKENS_FILTER)

        rmap: Dict[str, Dict] = {str(r.get("id","")): r for r in results if isinstance(r, dict)}
        for p in batch:
            pid = str(p.get("id",""))
            text = p.get("text","") or ""
            flagged = False
            reason = ""
            if pid in rmap:
                flagged = bool(rmap[pid].get("is_fact_checkable") is True)
                reason = rmap[pid].get("reason","")

            # Heuristic fallback
            if not flagged and looks_claimy(text):
                flagged = True
                reason = reason or "Heuristic: numeric/policy/claim-like language."

            if flagged:
                p["llm_fact_check_reason"] = reason
                kept.append(p)
    return kept

def score_posts_with_llm(posts: List[Dict], *, anthropic_api_key: str) -> List[Dict]:
    """
    Batch-score posts. Keeps posts even if scoring fails; missing scores default to 0.
    """
    if not posts:
        return []
    client = _anthropic_client(anthropic_api_key)

    scored: List[Dict] = []
    for batch in _chunks(posts, BATCH_SIZE):
        llm_items = [{"id": p.get("id",""), "text": p.get("text","")} for p in batch]
        results = _call_json_array(client, _score_prompt(llm_items), max_tokens=TOKENS_SCORE)
        rmap: Dict[str, Dict] = {str(r.get("id","")): r for r in results if isinstance(r, dict)}

        for p in batch:
            pid = str(p.get("id",""))
            r = rmap.get(pid, {})
            try:
                checkable = int(r.get("checkable_score", 0))
                importance = int(r.get("importance_score", 0))
            except Exception:
                checkable = 0
                importance = 0
            checkable = checkable if 1 <= checkable <= 10 else 0
            importance = importance if 1 <= importance <= 10 else 0
            p["checkable_score"] = checkable
            p["importance_score"] = importance
            p["llm_scoring_reason"] = r.get("scoring_reason", "")
            scored.append(p)
    return scored
