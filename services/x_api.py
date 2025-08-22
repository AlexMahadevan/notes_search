import time
from typing import List, Dict, Optional
from requests_oauthlib import OAuth1Session

X_API_URL = "https://api.x.com/2/notes/search/posts_eligible_for_notes"

def fetch_eligible_posts(
    api_key: str,
    api_secret: str,
    access_token: str,
    access_token_secret: str,
    *,
    test_mode: bool = True,
    max_results: int = 100,
    pages: int = 2,
    page_pause_sec: float = 0.5,
) -> List[Dict]:
    """
    Fetch posts eligible for Community Notes via X API (OAuth1).
    Returns a list of post dicts.
    """
    missing = [k for k, v in {
        "X_API_KEY": api_key,
        "X_API_KEY_SECRET": api_secret,
        "X_ACCESS_TOKEN": access_token,
        "X_ACCESS_TOKEN_SECRET": access_token_secret,
    }.items() if not str(v or "").strip()]
    if missing:
        raise RuntimeError(f"Missing X API credentials: {', '.join(missing)}")

    oauth = OAuth1Session(
        client_key=api_key,
        client_secret=api_secret,
        resource_owner_key=access_token,
        resource_owner_secret=access_token_secret,
    )

    params = {"test_mode": test_mode, "max_results": max_results}
    posts: List[Dict] = []
    pagination_token: Optional[str] = None

    for _ in range(pages):
        p = params.copy()
        if pagination_token:
            p["pagination_token"] = pagination_token
        resp = oauth.get(X_API_URL, params=p)
        if resp.status_code != 200:
            raise RuntimeError(f"X API {resp.status_code}: {resp.text}")
        data = resp.json() or {}
        batch = data.get("data") or []
        posts.extend(batch)

        meta = data.get("meta") or {}
        pagination_token = meta.get("next_token")
        if not pagination_token:
            break
        time.sleep(page_pause_sec)

    return posts
