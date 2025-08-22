import csv
import os
from typing import List, Dict

def ensure_list(x):
    return x if isinstance(x, list) else (x or [])

def add_links_to_rows(posts: List[Dict]) -> List[Dict]:
    rows = []
    for p in posts:
        r = dict(p)
        pid = r.get("id", "")
        if pid:
            r["post_link"] = f"https://x.com/i/status/{pid}"
        rows.append(r)
    return rows

def save_posts_to_csv(posts: List[Dict], filename: str) -> str:
    rows = add_links_to_rows(posts)
    if not rows:
        raise RuntimeError("No posts to save.")

    # union of all keys
    fieldnames = []
    for r in rows:
        for k in r.keys():
            if k not in fieldnames:
                fieldnames.append(k)

    # stable ordering for common fields
    preferred = [
        "id", "text", "post_link",
        "llm_fact_check_reason",
        "importance_score", "checkable_score", "llm_scoring_reason",
        "fact_check_questions", "search_keywords", "llm_aids_reason",
    ]
    fieldnames = preferred + [k for k in fieldnames if k not in preferred]

    path = os.path.abspath(filename)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return path
