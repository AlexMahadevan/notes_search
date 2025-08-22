import os
import streamlit as st
import pandas as pd
from typing import List, Dict, Optional

from services.x_api import fetch_eligible_posts
from services.llm import (
    filter_posts_with_llm,
    score_posts_with_llm,
)
from utils.io import save_posts_to_csv, ensure_list

# =========================================================
# Config: simple defaults (no sidebar)
# =========================================================
DEFAULT_TEST_MODE = True
DEFAULT_MAX_RESULTS = 100
DEFAULT_PAGES = 2
DEFAULT_EXPORT = "ranked_fact_checkable_x_posts.csv"

# =========================================================
# Safe secrets access
# =========================================================
def get_secret(name: str, default: str = "") -> str:
    try:
        return str(st.secrets.get(name, os.getenv(name, default)))
    except Exception:
        return os.getenv(name, default)

# =========================================================
# Page header
# =========================================================
st.set_page_config(page_title="X claim finder", layout="wide")

st.title("X claim finder")
st.caption(
    "Step 1: Pull X posts users have tagged for Community Notes. "
    "Step 2: Click **Analyze & Rank** to flag **fact-checkable** items and sort by "
    "**importance × checkability**. Reach TK."
)

# =========================================================
# Session state
# =========================================================
if "raw_posts" not in st.session_state:
    st.session_state.raw_posts: List[Dict] = []
if "filtered_posts" not in st.session_state:
    st.session_state.filtered_posts: List[Dict] = []
if "ranked_posts" not in st.session_state:
    st.session_state.ranked_posts: List[Dict] = []
if "advanced" not in st.session_state:
    st.session_state.advanced = {
        "test_mode": DEFAULT_TEST_MODE,
        "max_results": DEFAULT_MAX_RESULTS,
        "pages": DEFAULT_PAGES,
        "export_filename": DEFAULT_EXPORT,
    }

# =========================================================
# Credentials check (non-blocking)
# =========================================================
required = ["X_API_KEY", "X_API_KEY_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET", "ANTHROPIC_API_KEY"]
missing = [k for k in required if not str(get_secret(k, "")).strip()]
if missing:
    st.warning(
        "Missing credentials: " + ", ".join(missing) +
        ". Add them in `.streamlit/secrets.toml` or as environment variables."
    )

# =========================================================
# Controls (top-of-page buttons)
# =========================================================
col1, col2, col3 = st.columns([0.33, 0.33, 0.34])
fetch_clicked = col1.button("① Fetch posts", type="primary", use_container_width=True)
analyze_clicked = col2.button("② Analyze & Rank", use_container_width=True, disabled=not st.session_state.raw_posts)
export_clicked = col3.button("💾 Export CSV", use_container_width=True, disabled=not st.session_state.ranked_posts)

# Optional: Advanced options (inline, collapsed)
with st.expander("Advanced options (optional)"):
    a = st.session_state.advanced
    a["test_mode"] = st.checkbox("Use X test mode", value=a["test_mode"])
    a["max_results"] = st.number_input("Max results per page", 10, 500, a["max_results"], 10)
    a["pages"] = st.number_input("Pages to fetch", 1, 20, a["pages"], 1)
    a["export_filename"] = st.text_input("CSV filename", value=a["export_filename"])
    st.session_state.advanced = a

# =========================================================
# Helper: build table rows with clickable links
# =========================================================
def as_table_rows(posts: List[Dict]) -> pd.DataFrame:
    if not posts:
        return pd.DataFrame()
    rows = []
    for p in posts:
        pid = p.get("id", "")
        text = (p.get("text", "") or "").strip()
        rows.append({
            "Tweet": text[:220] + ("…" if len(text) > 220 else ""),
            "Open": f"https://x.com/i/status/{pid}" if pid else "",
            # analysis fields may be blank until step ② runs
            "Reason (filter)": p.get("llm_fact_check_reason", ""),
            "Importance": p.get("importance_score", ""),
            "Checkability": p.get("checkable_score", ""),
            "Scoring reason": p.get("llm_scoring_reason", ""),
        })
    return pd.DataFrame(rows)

def show_clickable_table(title: str, posts: List[Dict], cols: Optional[list] = None):
    df = as_table_rows(posts)
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

# =========================================================
# Step ①: Fetch (no LLM) → show preview immediately
# =========================================================
if fetch_clicked:
    with st.spinner("Fetching eligible posts from X…"):
        try:
            posts = fetch_eligible_posts(
                api_key=get_secret("X_API_KEY", ""),
                api_secret=get_secret("X_API_KEY_SECRET", ""),
                access_token=get_secret("X_ACCESS_TOKEN", ""),
                access_token_secret=get_secret("X_ACCESS_TOKEN_SECRET", ""),
                test_mode=st.session_state.advanced["test_mode"],
                max_results=int(st.session_state.advanced["max_results"]),
                pages=int(st.session_state.advanced["pages"]),
            )
        except Exception as e:
            st.error(f"X API error: {e}")
            posts = []
    st.session_state.raw_posts = ensure_list(posts)
    st.session_state.filtered_posts = []
    st.session_state.ranked_posts = []
    st.success(f"Fetched {len(st.session_state.raw_posts)} posts.")

# Always show a bigger preview if we have raw posts
if st.session_state.raw_posts:
    preview_cols = ["Tweet", "Open"]
    st.markdown("#### Preview (first 100 from X)")
    show_clickable_table(
        "",
        st.session_state.raw_posts[:100],  # <-- show up to 100 posts now
        cols=preview_cols
    )

# =========================================================
# Step ②: Analyze & Rank (on demand) → filter + score + sort
# =========================================================
if analyze_clicked:
    if not get_secret("ANTHROPIC_API_KEY", ""):
        st.error("Missing ANTHROPIC_API_KEY. Set it and try again.")
    elif not st.session_state.raw_posts:
        st.warning("Fetch posts first.")
    else:
        with st.spinner("Identifying fact-checkable posts…"):
            filtered = filter_posts_with_llm(
                st.session_state.raw_posts,
                anthropic_api_key=get_secret("ANTHROPIC_API_KEY", ""),
            )
        st.session_state.filtered_posts = filtered

        with st.spinner("Scoring by importance × checkability…"):
            scored = score_posts_with_llm(
                st.session_state.filtered_posts,
                anthropic_api_key=get_secret("ANTHROPIC_API_KEY", ""),
            )
            ranked = sorted(
                scored,
                key=lambda p: (p.get("importance_score", 0), p.get("checkable_score", 0)),
                reverse=True,
            )
        st.session_state.ranked_posts = ranked

        st.success(f"Analyzed {len(st.session_state.ranked_posts)} posts.")

# Show results when available
if st.session_state.filtered_posts:
    show_clickable_table("Fact-checkable posts", st.session_state.filtered_posts, cols=["Tweet", "Open", "Reason (filter)"])

if st.session_state.ranked_posts:
    show_clickable_table("Ranked posts", st.session_state.ranked_posts,
                         cols=["Tweet", "Open", "Importance", "Checkability", "Scoring reason"])

# =========================================================
# Export CSV (after analysis)
# =========================================================
if export_clicked:
    if not st.session_state.ranked_posts:
        st.warning("Analyze & Rank first.")
    else:
        path = save_posts_to_csv(st.session_state.ranked_posts, st.session_state.advanced["export_filename"])
        st.success(f"Saved to {path}")
        with open(path, "rb") as f:
            st.download_button("Download CSV", data=f, file_name=st.session_state.advanced["export_filename"], mime="text/csv")

st.markdown("---")
st.caption("**Important:** This tool supports human judgment. It will only augment — never replace a real fact-checker:).")
