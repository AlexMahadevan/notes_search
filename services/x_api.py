# Updated x_api.py with rate limit handling

import math
import time
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta
from requests_oauthlib import OAuth1Session

# Endpoints
X_ELIGIBLE_URL = "https://api.x.com/2/notes/search/posts_eligible_for_notes"
X_TWEETS_LOOKUP = "https://api.x.com/2/tweets"

# Rate limit tracking
RATE_LIMIT_STATE = {
    "remaining": None,
    "reset_time": None,
    "last_checked": None
}

def _oauth(api_key: str, api_secret: str, token: str, token_secret: str) -> OAuth1Session:
    if not all([api_key, api_secret, token, token_secret]):
        raise RuntimeError("Missing X API credentials.")
    return OAuth1Session(
        client_key=api_key,
        client_secret=api_secret,
        resource_owner_key=token,
        resource_owner_secret=token_secret,
    )

def check_rate_limit_headers(response):
    """Extract and store rate limit info from response headers"""
    global RATE_LIMIT_STATE
    
    if 'x-rate-limit-remaining' in response.headers:
        RATE_LIMIT_STATE["remaining"] = int(response.headers['x-rate-limit-remaining'])
        RATE_LIMIT_STATE["last_checked"] = datetime.now()
        
        print(f"[Rate Limit] Remaining: {RATE_LIMIT_STATE['remaining']}")
    
    if 'x-rate-limit-reset' in response.headers:
        reset_timestamp = int(response.headers['x-rate-limit-reset'])
        RATE_LIMIT_STATE["reset_time"] = datetime.fromtimestamp(reset_timestamp)
        
        print(f"[Rate Limit] Resets at: {RATE_LIMIT_STATE['reset_time']}")
    
    return RATE_LIMIT_STATE

def should_skip_metrics_fetch():
    """Check if we should skip metrics fetching due to rate limits"""
    global RATE_LIMIT_STATE
    
    # If we know we're rate limited
    if RATE_LIMIT_STATE["remaining"] is not None and RATE_LIMIT_STATE["remaining"] <= 5:
        if RATE_LIMIT_STATE["reset_time"] and datetime.now() < RATE_LIMIT_STATE["reset_time"]:
            wait_time = (RATE_LIMIT_STATE["reset_time"] - datetime.now()).total_seconds()
            print(f"[Rate Limit] Only {RATE_LIMIT_STATE['remaining']} requests left. Reset in {wait_time:.0f} seconds")
            return True, f"Rate limited. Resets in {wait_time:.0f}s"
    
    return False, ""

def fetch_eligible_posts(
    *,
    api_key: str,
    api_secret: str,
    access_token: str,
    access_token_secret: str,
    test_mode: bool = True,
    max_results: int = 100,
    pages: int = 1,
    fetch_metrics: bool = False,
    compute_reach: bool = False,
) -> List[Dict]:
    """
    Fetch posts eligible for Community Notes with better rate limit handling
    """
    sess = _oauth(api_key, api_secret, access_token, access_token_secret)
    
    all_posts: List[Dict] = []
    pagination_token: Optional[str] = None
    page_count = 0
    
    # First, try to get metrics in the initial request
    while True:
        params = {
            "test_mode": bool(test_mode),
            "max_results": int(max_results),
            # Try to get metrics directly in the community notes call
            "tweet.fields": "public_metrics,author_id,created_at",
            "expansions": "author_id",
            "user.fields": "public_metrics"
        }
        if pagination_token:
            params["pagination_token"] = pagination_token
        
        try:
            resp = sess.get(X_ELIGIBLE_URL, params=params, timeout=30)
            check_rate_limit_headers(resp)  # Track rate limits
            
            if resp.status_code == 429:  # Rate limited
                print("[Rate Limit] Hit rate limit on eligible posts endpoint")
                wait_time = 60  # Default wait
                if 'retry-after' in resp.headers:
                    wait_time = int(resp.headers['retry-after'])
                print(f"Waiting {wait_time} seconds...")
                time.sleep(wait_time)
                continue
                
            if resp.status_code != 200:
                print(f"[API Error] Status {resp.status_code}: {resp.text[:200]}")
                break
                
            payload = resp.json() or {}
        except Exception as e:
            print(f"[API Exception] {e}")
            break
        
        batch = payload.get("data") or []
        if not batch:
            break
        
        # Check if metrics came with the initial response
        for post in batch:
            if "public_metrics" in post:
                metrics = post["public_metrics"]
                post["tweet_retweet_count"] = metrics.get("retweet_count", 0)
                post["tweet_like_count"] = metrics.get("like_count", 0)
                post["tweet_reply_count"] = metrics.get("reply_count", 0)
                post["tweet_quote_count"] = metrics.get("quote_count", 0)
                post["__metrics_hydrated__"] = True
                print(f"[Success] Got metrics for post {post.get('id')} from initial request!")
        
        all_posts.extend(batch)
        page_count += 1
        if page_count >= int(pages):
            break
        
        meta = payload.get("meta") or {}
        pagination_token = meta.get("next_token")
        if not pagination_token:
            break
        
        time.sleep(1)  # Increased delay between pages
    
    # Only try separate metrics fetch if needed and not rate limited
    needs_metrics = fetch_metrics and all_posts and not any(p.get("__metrics_hydrated__") for p in all_posts)
    
    if needs_metrics:
        skip, reason = should_skip_metrics_fetch()
        if skip:
            print(f"[Skip Metrics] {reason}")
            for p in all_posts[:1]:
                p["__metrics_hydrated__"] = False
                p["__metrics_reason__"] = reason
        else:
            posts, hydrated, reason = hydrate_public_metrics_verbose(
                all_posts,
                api_key=api_key,
                api_secret=api_secret,
                access_token=access_token,
                access_token_secret=access_token_secret,
            )
            all_posts = posts
            for p in all_posts[:1]:
                p["__metrics_hydrated__"] = bool(hydrated)
                if not hydrated and reason:
                    p["__metrics_reason__"] = reason
        
        if compute_reach:
            for p in all_posts:
                if p.get("__metrics_hydrated__"):
                    p["reach_score"] = compute_reach_score(p)
                else:
                    p["reach_score"] = 0
    
    return all_posts

def _chunk_ids(ids: List[str], n: int = 100) -> List[List[str]]:
    return [ids[i:i+n] for i in range(0, len(ids), n)]

def _safe_int(x, default=0):
    try:
        return int(x)
    except Exception:
        try:
            return int(float(x))
        except Exception:
            return default

def hydrate_public_metrics_verbose(
    posts: List[Dict],
    *,
    api_key: str,
    api_secret: str,
    access_token: str,
    access_token_secret: str,
) -> Tuple[List[Dict], bool, str]:
    """
    Enhanced metrics hydration with better error handling and rate limit awareness
    """
    if not posts:
        return posts, False, "no posts"
    
    sess = _oauth(api_key, api_secret, access_token, access_token_secret)
    
    id_map: Dict[str, Dict] = {str(p.get("id", "")): p for p in posts if p.get("id")}
    ids = [i for i in id_map.keys() if i]
    if not ids:
        return posts, False, "missing tweet ids"
    
    base_params = {
        "tweet.fields": "public_metrics,author_id",
        "user.fields": "public_metrics",
        "expansions": "author_id",
    }
    
    hydrated_any = False
    last_error = ""
    batches_processed = 0
    
    for batch in _chunk_ids(ids, 100):
        # Check if we should stop due to rate limits
        skip, reason = should_skip_metrics_fetch()
        if skip:
            print(f"[Rate Limit] Stopping after {batches_processed} batches: {reason}")
            last_error = reason
            break
        
        params = dict(base_params)
        params["ids"] = ",".join(batch)
        
        retry_count = 0
        max_retries = 3
        
        while retry_count < max_retries:
            try:
                r = sess.get(X_TWEETS_LOOKUP, params=params, timeout=30)
                check_rate_limit_headers(r)  # Track rate limits
                
                if r.status_code == 429:  # Rate limited
                    retry_after = int(r.headers.get('retry-after', 60))
                    print(f"[Rate Limit] Hit limit. Waiting {retry_after} seconds...")
                    
                    # Store the error and stop trying
                    last_error = f"Rate limited. Try again in {retry_after}s"
                    
                    # Don't wait if it's too long
                    if retry_after > 300:  # More than 5 minutes
                        print("[Rate Limit] Wait time too long, stopping metrics fetch")
                        return posts, hydrated_any, last_error
                    
                    time.sleep(retry_after)
                    retry_count += 1
                    continue
                
                if r.status_code != 200:
                    error_detail = r.json().get("detail", r.text[:200]) if r.text else "Unknown error"
                    last_error = f"/2/tweets {r.status_code}: {error_detail}"
                    print(f"[API Error] {last_error}")
                    break  # Don't retry on non-429 errors
                
                j = r.json() or {}
                
                # Process successful response
                for t in (j.get("data") or []):
                    tid = str(t.get("id", ""))
                    pm = (t.get("public_metrics") or {})
                    p = id_map.get(tid)
                    if not p:
                        continue
                    p["tweet_like_count"]    = _safe_int(pm.get("like_count", 0))
                    p["tweet_retweet_count"] = _safe_int(pm.get("retweet_count", 0))
                    p["tweet_reply_count"]   = _safe_int(pm.get("reply_count", 0))
                    p["tweet_quote_count"]   = _safe_int(pm.get("quote_count", 0))
                    p["author_id"]           = t.get("author_id", p.get("author_id"))
                    hydrated_any = True
                    print(f"[Success] Got metrics for tweet {tid}: {p['tweet_retweet_count']} RTs")
                
                # Process user data
                users = j.get("includes", {}).get("users") or []
                user_map = {u.get("id"): u for u in users}
                for t in (j.get("data") or []):
                    aid = t.get("author_id")
                    u = user_map.get(aid or "")
                    if not u:
                        continue
                    pmu = u.get("public_metrics") or {}
                    tid = str(t.get("id", ""))
                    p = id_map.get(tid)
                    if p:
                        p["author_followers_count"] = _safe_int(pmu.get("followers_count", 0))
                
                batches_processed += 1
                break  # Success, move to next batch
                
            except Exception as e:
                last_error = f"/2/tweets exception: {e!r}"
                print(f"[API Exception] {last_error}")
                retry_count += 1
                if retry_count < max_retries:
                    time.sleep(2 ** retry_count)  # Exponential backoff
        
        # Add delay between batches to avoid rate limits
        time.sleep(1)
    
    if not hydrated_any and not last_error:
        last_error = "no metrics returned (check API access tier)"
    
    print(f"[Summary] Hydrated metrics: {hydrated_any}, Batches: {batches_processed}, Error: {last_error}")
    
    return posts, hydrated_any, last_error

def compute_reach_score(post: Dict) -> float:
    """
    Convert raw metrics into a dampened 0..10 reach score.
    """
    likes   = _safe_int(post.get("tweet_like_count", 0))
    rts     = _safe_int(post.get("tweet_retweet_count", 0))
    replies = _safe_int(post.get("tweet_reply_count", 0))
    quotes  = _safe_int(post.get("tweet_quote_count", 0))
    foll    = _safe_int(post.get("author_followers_count", 0))
    
    raw = likes + 2*rts + 1.5*replies + 2*quotes + 0.002*foll
    if raw <= 0:
        return 0.0
    score = min(10.0, (math.log1p(raw) / math.log(1000.0)) * 10.0)
    return round(score, 1)