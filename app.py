# app.py — ranking uses RETWEETS (not reach)

import os
import sys
import math
import pathlib
import streamlit as st
import pandas as pd
from typing import List, Dict, Optional

ROOT = pathlib.Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.x_api import (
    fetch_eligible_posts,
)
from services.llm import filter_posts_with_llm, score_posts_with_llm
from utils.io import save_posts_to_csv, ensure_list


def get_secret(name: str, default: str = "") -> str:
    try:
        val = st.secrets.get(name, None)
        if val is None or str(val).strip() == "":
            val = os.getenv(name, default)
    except Exception:
        val = os.getenv(name, default)
    return (str(val) if val is not None else "").strip()


st.set_page_config(page_title="X posts finder", layout="wide")

st.title("X posts finder")
st.caption(
    "Step 1: Fetch X posts flagged for Community Notes.  \n"
    "Step 2: Analyze to flag **fact-checkable** items and rank by "
    "**importance × checkability × retweets**."
)

ss = st.session_state
ss.setdefault("raw_posts", [])
ss.setdefault("filtered_posts", [])
ss.setdefault("ranked_posts", [])
ss.setdefault("did_fetch", False)
ss.setdefault("did_analyze", False)
ss.setdefault("adv_test_mode", True)
ss.setdefault("adv_max_results", 100)
ss.setdefault("adv_pages", 2)
ss.setdefault("export_name", "ranked_fact_checkable_x_posts.csv")
ss.setdefault("enable_high_reach_filter", False)   # keeps your threshold-by-retweets option
ss.setdefault("min_retweets_threshold", 25)


def _do_fetch():
    with st.spinner("Fetching eligible posts from X…"):
        try:
            posts = fetch_eligible_posts(
                api_key=get_secret("X_API_KEY", ""),
                api_secret=get_secret("X_API_KEY_SECRET", ""),
                access_token=get_secret("X_ACCESS_TOKEN", ""),
                access_token_secret=get_secret("X_ACCESS_TOKEN_SECRET", ""),
                test_mode=ss.adv_test_mode,
                max_results=int(ss.adv_max_results),
                pages=int(ss.adv_pages),
                fetch_metrics=True,                # hydrate metrics on fetch (retweets, etc.)
                compute_reach=False,               # no longer used in UI/ranking
            )
        except Exception as e:
            st.error(f"X API error: {e}")
            posts = []

    ss.raw_posts = ensure_list(posts)
    ss.filtered_posts = []
    ss.ranked_posts = []
    ss.did_fetch = True
    ss.did_analyze = False

    # Metrics availability banner
    metrics_hydrated = False
    reason = ""
    if ss.raw_posts:
        probe = ss.raw_posts[0]
        metrics_hydrated = bool(probe.get("__metrics_hydrated__"))
        reason = str(probe.get("__metrics_reason__", "")).strip()

    if not metrics_hydrated:
        st.warning(
            "Tweet metrics (retweets/likes/replies/quotes) could not be hydrated. "
            "This is usually due to access tier/permissions for `/2/tweets` lookup. "
            "We’ll still show posts (retweets will be 0)."
            + (f" • Detail: {reason}" if reason else "")
        )


def _apply_high_reach_filter(posts: List[Dict]) -> List[Dict]:
    """Filter by retweets threshold if enabled."""
    if not ss.enable_high_reach_filter:
        return posts
    threshold = int(ss.min_retweets_threshold or 0)
    return [p for p in posts if int(p.get("tweet_retweet_count", 0) or 0) >= threshold]


def _retweet_score(post: Dict) -> float:
    """
    Convert raw retweet count to a dampened 0..10 score using log scaling.
    Keeps huge virals from dominating while rewarding amplification.
    """
    rts = int(post.get("tweet_retweet_count", 0) or 0)
    if rts <= 0:
        return 0.0
    return min(10.0, (math.log1p(rts) / math.log(1000.0)) * 10.0)


def _do_analyze():
    if not ss.raw_posts:
        st.warning("Fetch posts first.")
        return

    analysis_input = _apply_high_reach_filter(ss.raw_posts)

    with st.spinner("Identifying fact-checkable posts…"):
        filtered = filter_posts_with_llm(
            analysis_input,
            anthropic_api_key=get_secret("ANTHROPIC_API_KEY", ""),
        )
    ss.filtered_posts = filtered

    with st.spinner("Scoring by importance × checkability…"):
        scored = score_posts_with_llm(
            ss.filtered_posts,
            anthropic_api_key=get_secret("ANTHROPIC_API_KEY", ""),
        )

    # FINAL SCORE uses retweet_score (not reach)
    for p in scored:
        imp = int(p.get("importance_score", 0) or 0)
        chk = int(p.get("checkable_score", 0) or 0)
        rt  = _retweet_score(p)
        # Weighted blend — feel free to tweak
        p["_final_score"] = 0.5 * imp + 0.3 * chk + 0.2 * rt
        p["retweet_score"] = rt  # expose for table display

    ranked = sorted(scored, key=lambda x: x.get("_final_score", 0), reverse=True)
    ranked = _apply_high_reach_filter(ranked)

    ss.ranked_posts = ranked
    ss.did_analyze = True


def _do_export():
    if not ss.ranked_posts:
        st.warning("Analyze & rank first.")
        return
    path = save_posts_to_csv(ss.ranked_posts, ss.export_name)
    st.success(f"Saved to {path}")
    with open(path, "rb") as f:
        st.download_button("Download CSV", data=f, file_name=ss.export_name, mime="text/csv")


# Controls
c1, c2, c3, c4 = st.columns([0.22, 0.22, 0.22, 0.34])
c1.button("Fetch posts", type="primary", use_container_width=True, key="btn_fetch", on_click=_do_fetch)
c2.button("Analyze & rank", use_container_width=True, key="btn_analyze",
          disabled=not ss.did_fetch, on_click=_do_analyze)
c3.button("Export CSV", use_container_width=True, key="btn_export",
          disabled=not ss.ranked_posts, on_click=_do_export)

with c4.expander("Options"):
    ss.adv_test_mode = st.checkbox("Use X test mode", value=ss.adv_test_mode)
    ss.adv_max_results = st.number_input("Max results per page", 10, 500, ss.adv_max_results, 10)
    ss.adv_pages = st.number_input("Pages to fetch", 1, 10, ss.adv_pages, 1)
    st.markdown("---")
    ss.enable_high_reach_filter = st.checkbox(
        "Filter to high-retweet posts only",
        value=ss.enable_high_reach_filter
    )
    ss.min_retweets_threshold = st.number_input(
        "Minimum retweets (threshold)", 0, 100000, ss.min_retweets_threshold, 5
    )
    st.caption("Tip: Start around 25–100 to surface truly viral items.")
    st.markdown("---")
    ss.export_name = st.text_input("CSV filename", value=ss.export_name)

st.divider()


def _rows(posts: List[Dict]) -> pd.DataFrame:
    if not posts:
        return pd.DataFrame()
    rows = []
    for p in posts:
        pid = p.get("id", "")
        text = (p.get("text", "") or "").strip()
        rows.append({
            "Tweet": text[:220] + ("…" if len(text) > 220 else ""),
            "Open": f"https://x.com/i/status/{pid}" if pid else "",
            "Retweets": int(p.get("tweet_retweet_count", 0) or 0),
            "Likes": int(p.get("tweet_like_count", 0) or 0),
            "Replies": int(p.get("tweet_reply_count", 0) or 0),
            "Quotes": int(p.get("tweet_quote_count", 0) or 0),
            "Reason (filter)": p.get("llm_fact_check_reason", ""),
            "Importance": p.get("importance_score", ""),
            "Checkability": p.get("checkable_score", ""),
            "Retweet score": p.get("retweet_score", ""),
            "Scoring reason": p.get("llm_scoring_reason", ""),
        })
    return pd.DataFrame(rows)


def _show_table(title: str, posts: List[Dict], cols: Optional[list] = None):
    df = _rows(posts)
    if df.empty:
        return
    if cols:
        df = df[cols]
    if title:
        st.subheader(title)
    st.dataframe(
        df,
        use_container_width=True,
        hide_index=True,
        column_config={"Open": st.column_config.LinkColumn("Open", display_text="Open")},
    )


# A. Preview
st.header("A. Preview")
if ss.did_fetch and ss.raw_posts:
    preview_list = _apply_high_reach_filter(ss.raw_posts)
    preview_cols = ["Tweet", "Open", "Retweets", "Likes", "Replies", "Quotes"]
    _show_table("First 100 from X", preview_list[:100], cols=preview_cols)
else:
    st.info("Click **Fetch posts** to load a preview.")

st.divider()

# B. Analysis
st.header("B. Analysis")
if not ss.did_fetch:
    st.info("Fetch posts first, then click **Analyze & rank**.")
else:
    if not ss.did_analyze:
        st.info("Click **Analyze & rank** to flag fact-checkable posts and see rankings here.")
    else:
        tabs = st.tabs(["Fact-checkable", "Ranked"])
        with tabs[0]:
            _show_table("", ss.filtered_posts, cols=["Tweet", "Open", "Reason (filter)"])
        with tabs[1]:
            _show_table(
                "",
                ss.ranked_posts,
                cols=["Tweet", "Open", "Retweets", "Importance", "Checkability", "Retweet score", "Scoring reason"],
            )

st.markdown("---")
st.caption("This tool assists human judgment. Always verify with primary sources.")
