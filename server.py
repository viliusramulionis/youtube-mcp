"""YouTube MCP server.

Runs locally (stdio, `python server.py`) or hosted on Prefect Horizon
(entrypoint `server.py:mcp`).

Credentials (environment variables):
  YOUTUBE_API_KEY          read tools (search, video, channel, comments)
  YOUTUBE_CLIENT_ID        \
  YOUTUBE_CLIENT_SECRET     > account actions (OAuth); get them with `python server.py print-env`
  YOUTUBE_REFRESH_TOKEN    /
  YOUTUBE_READ_ONLY=1      hide all account-action tools
  WEBSHARE_PROXY_USERNAME / WEBSHARE_PROXY_PASSWORD, or TRANSCRIPT_PROXY_URL
                           proxy for transcripts (YouTube often blocks cloud IPs)

Locally, OAuth can instead come from token.json created by `python server.py auth`.
"""
import os
import re
import sys
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api import _errors as yt_errors

try:  # standalone FastMCP (required by Horizon)
    from fastmcp import FastMCP
except ImportError:
    try:  # mcp SDK 2.x
        from mcp.server.mcpserver import MCPServer as FastMCP
    except ImportError:  # mcp SDK 1.x
        from mcp.server.fastmcp import FastMCP

BASE = Path(os.environ.get("YOUTUBE_MCP_DIR", Path(__file__).resolve().parent))
CLIENT_SECRET = BASE / "client_secret.json"
TOKEN = BASE / "token.json"
TOKEN_URI = "https://oauth2.googleapis.com/token"
SCOPES = [
    "https://www.googleapis.com/auth/youtube.force-ssl",
    "https://www.googleapis.com/auth/youtube.upload",
]
API_KEY = os.environ.get("YOUTUBE_API_KEY")
READ_ONLY = os.environ.get("YOUTUBE_READ_ONLY", "").lower() in ("1", "true", "yes")

mcp = FastMCP("youtube")


# ---------- credentials ----------

_creds_cache: Credentials | None = None


def _oauth_creds() -> Credentials | None:
    """OAuth from env vars (hosted) or token.json (local). Refreshed in memory."""
    global _creds_cache
    creds = _creds_cache
    if creds is None:
        refresh = os.environ.get("YOUTUBE_REFRESH_TOKEN")
        if refresh:
            creds = Credentials(
                token=None,
                refresh_token=refresh,
                client_id=os.environ.get("YOUTUBE_CLIENT_ID"),
                client_secret=os.environ.get("YOUTUBE_CLIENT_SECRET"),
                token_uri=TOKEN_URI,
                scopes=SCOPES,
            )
        elif TOKEN.exists():
            creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
        else:
            return None
    if not creds.valid and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError as e:
            raise RuntimeError("Google rejected the refresh token (expired or revoked). "
                               "Run `python server.py auth` again and update YOUTUBE_REFRESH_TOKEN.") from e
        if not os.environ.get("YOUTUBE_REFRESH_TOKEN") and TOKEN.exists():
            TOKEN.write_text(creds.to_json())
    _creds_cache = creds
    return creds if creds.valid else None


def _yt(write: bool = False):
    if write:
        creds = _oauth_creds()
        if not creds:
            raise RuntimeError("No OAuth credentials. Set YOUTUBE_CLIENT_ID/SECRET/REFRESH_TOKEN "
                               "or run locally: python server.py auth")
        return build("youtube", "v3", credentials=creds, cache_discovery=False)
    if API_KEY:
        return build("youtube", "v3", developerKey=API_KEY, cache_discovery=False)
    creds = _oauth_creds()
    if creds:
        return build("youtube", "v3", credentials=creds, cache_discovery=False)
    raise RuntimeError("Set YOUTUBE_API_KEY (or OAuth credentials)")


# ---------- helpers ----------

_VID_RE = re.compile(r"(?:v=|youtu\.be/|shorts/|embed/|live/)([A-Za-z0-9_-]{11})")


def _vid(video: str) -> str:
    """Accept a video ID or any common YouTube URL."""
    m = _VID_RE.search(video)
    return m.group(1) if m else video.strip()


def _paged(request_fn, max_results: int, **kwargs) -> list:
    items, token = [], None
    while len(items) < max_results:
        resp = request_fn(maxResults=min(50, max_results - len(items)), pageToken=token, **kwargs).execute()
        items += resp.get("items", [])
        token = resp.get("nextPageToken")
        if not token:
            break
    return items


def _video_summary(it: dict, vid: str | None = None) -> dict:
    s = it.get("snippet", {})
    return {
        "video_id": vid or it.get("id"),
        "title": s.get("title"),
        "channel": s.get("channelTitle"),
        "channel_id": s.get("channelId"),
        "published": s.get("publishedAt"),
        "description": (s.get("description") or "")[:300],
    }


def _transcript_api() -> YouTubeTranscriptApi:
    from youtube_transcript_api import proxies
    if os.environ.get("WEBSHARE_PROXY_USERNAME"):
        return YouTubeTranscriptApi(proxy_config=proxies.WebshareProxyConfig(
            proxy_username=os.environ["WEBSHARE_PROXY_USERNAME"],
            proxy_password=os.environ.get("WEBSHARE_PROXY_PASSWORD", "")))
    if os.environ.get("TRANSCRIPT_PROXY_URL"):
        url = os.environ["TRANSCRIPT_PROXY_URL"]
        return YouTubeTranscriptApi(proxy_config=proxies.GenericProxyConfig(http_url=url, https_url=url))
    return YouTubeTranscriptApi()


_BLOCKED = tuple(e for e in (getattr(yt_errors, "RequestBlocked", None),
                             getattr(yt_errors, "IpBlocked", None)) if e)


# ---------- read: search & video ----------

@mcp.tool()
def search_videos(query: str, max_results: int = 10, order: str = "relevance",
                  channel_id: str | None = None, published_after: str | None = None) -> list[dict]:
    """Search YouTube videos. order: relevance|date|viewCount|rating.
    published_after: RFC3339, e.g. 2026-01-01T00:00:00Z. Costs 100 quota units per page."""
    kw = dict(part="snippet", q=query, type="video", order=order)
    if channel_id:
        kw["channelId"] = channel_id
    if published_after:
        kw["publishedAfter"] = published_after
    items = _paged(_yt().search().list, max_results, **kw)
    return [_video_summary(i, i["id"]["videoId"]) for i in items]


@mcp.tool()
def get_video(video: str) -> dict:
    """Full details for a video (ID or URL): metadata, duration, stats, privacy status."""
    resp = _yt().videos().list(part="snippet,contentDetails,statistics,status", id=_vid(video)).execute()
    if not resp.get("items"):
        raise ValueError("Video not found")
    it = resp["items"][0]
    s = it["snippet"]
    return {
        "video_id": it["id"],
        "title": s["title"],
        "description": s.get("description"),
        "tags": s.get("tags", []),
        "category_id": s.get("categoryId"),
        "channel": s["channelTitle"],
        "channel_id": s["channelId"],
        "published": s["publishedAt"],
        "duration": it["contentDetails"]["duration"],
        "statistics": it.get("statistics", {}),
        "privacy": it["status"].get("privacyStatus"),
    }


@mcp.tool()
def get_transcript(video: str, languages: list[str] | None = None, with_timestamps: bool = False) -> str:
    """Transcript/subtitles of a video (ID or URL). languages: preference order, default ["lt", "en"]."""
    vid = _vid(video)
    api = _transcript_api()
    try:
        fetched = api.fetch(vid, languages=languages or ["lt", "en"])
    except yt_errors.NoTranscriptFound:
        avail = [f"{t.language_code} ({'auto' if t.is_generated else 'manual'})" for t in api.list(vid)]
        raise ValueError(f"No transcript in requested languages. Available: {', '.join(avail)}")
    except yt_errors.TranscriptsDisabled:
        raise ValueError("Transcripts are disabled for this video")
    except _BLOCKED:
        raise ValueError("YouTube blocked this server's IP for transcripts. "
                         "Set WEBSHARE_PROXY_USERNAME/PASSWORD or TRANSCRIPT_PROXY_URL.")
    if with_timestamps:
        return "\n".join(f"[{int(s.start // 60):02d}:{int(s.start % 60):02d}] {s.text}" for s in fetched.snippets)
    return " ".join(s.text for s in fetched.snippets)


@mcp.tool()
def get_comments(video: str, max_results: int = 20, order: str = "relevance") -> list[dict]:
    """Top-level comments of a video. order: relevance|time. Returns comment_id usable with reply_to_comment."""
    items = _paged(_yt().commentThreads().list, max_results, part="snippet",
                   videoId=_vid(video), order=order, textFormat="plainText")
    out = []
    for t in items:
        c = t["snippet"]["topLevelComment"]
        s = c["snippet"]
        out.append({"comment_id": c["id"], "author": s["authorDisplayName"], "text": s["textDisplay"],
                    "likes": s["likeCount"], "replies": t["snippet"]["totalReplyCount"],
                    "published": s["publishedAt"]})
    return out


# ---------- read: channels & playlists ----------

def _channel_kwargs(channel: str) -> dict:
    if channel == "mine":
        return {"mine": True}
    if channel.startswith("@"):
        return {"forHandle": channel}
    return {"id": channel}


@mcp.tool()
def get_channel(channel: str) -> dict:
    """Channel info + stats. channel: channel ID (UC...), @handle, or "mine" (needs OAuth)."""
    yt = _yt(write=channel == "mine")
    resp = yt.channels().list(part="snippet,statistics,contentDetails", **_channel_kwargs(channel)).execute()
    if not resp.get("items"):
        raise ValueError("Channel not found")
    it = resp["items"][0]
    return {
        "channel_id": it["id"],
        "title": it["snippet"]["title"],
        "handle": it["snippet"].get("customUrl"),
        "description": it["snippet"].get("description"),
        "created": it["snippet"]["publishedAt"],
        "statistics": it["statistics"],
        "uploads_playlist_id": it["contentDetails"]["relatedPlaylists"]["uploads"],
    }


@mcp.tool()
def list_channel_videos(channel: str, max_results: int = 25) -> list[dict]:
    """Latest uploads of a channel (ID, @handle, or "mine"), newest first."""
    uploads = get_channel(channel)["uploads_playlist_id"]
    return get_playlist_items(uploads, max_results)


@mcp.tool()
def list_playlists(channel: str = "mine", max_results: int = 50) -> list[dict]:
    """Playlists of a channel (channel ID or "mine")."""
    kw = {"mine": True} if channel == "mine" else {"channelId": channel}
    items = _paged(_yt(write=channel == "mine").playlists().list, max_results,
                   part="snippet,contentDetails,status", **kw)
    return [{"playlist_id": p["id"], "title": p["snippet"]["title"],
             "videos": p["contentDetails"]["itemCount"], "privacy": p["status"]["privacyStatus"]}
            for p in items]


@mcp.tool()
def get_playlist_items(playlist_id: str, max_results: int = 50) -> list[dict]:
    """Videos in a playlist. Returns playlist_item_id usable with remove_from_playlist."""
    # Prefer OAuth when available so private playlists of your own account work too.
    yt = _yt(write=True) if _oauth_creds() else _yt()
    items = _paged(yt.playlistItems().list, max_results, part="snippet", playlistId=playlist_id)
    out = []
    for i in items:
        d = _video_summary(i, i["snippet"]["resourceId"].get("videoId"))
        d["playlist_item_id"] = i["id"]
        out.append(d)
    return out


# ---------- write: account actions (OAuth) ----------

def post_comment(video: str, text: str) -> dict:
    """Post a public top-level comment on a video as the authorized account."""
    body = {"snippet": {"videoId": _vid(video),
                        "topLevelComment": {"snippet": {"textOriginal": text}}}}
    r = _yt(write=True).commentThreads().insert(part="snippet", body=body).execute()
    return {"comment_id": r["snippet"]["topLevelComment"]["id"]}


def reply_to_comment(comment_id: str, text: str) -> dict:
    """Reply publicly to a comment (comment_id from get_comments)."""
    body = {"snippet": {"parentId": comment_id, "textOriginal": text}}
    r = _yt(write=True).comments().insert(part="snippet", body=body).execute()
    return {"comment_id": r["id"]}


def rate_video(video: str, rating: str) -> str:
    """Rate a video: like | dislike | none (removes rating)."""
    if rating not in ("like", "dislike", "none"):
        raise ValueError("rating must be like, dislike or none")
    _yt(write=True).videos().rate(id=_vid(video), rating=rating).execute()
    return f"Rated {rating}"


def create_playlist(title: str, description: str = "", privacy: str = "private") -> dict:
    """Create a playlist. privacy: private|unlisted|public."""
    body = {"snippet": {"title": title, "description": description},
            "status": {"privacyStatus": privacy}}
    r = _yt(write=True).playlists().insert(part="snippet,status", body=body).execute()
    return {"playlist_id": r["id"]}


def add_to_playlist(playlist_id: str, video: str) -> dict:
    """Add a video (ID or URL) to a playlist."""
    body = {"snippet": {"playlistId": playlist_id,
                        "resourceId": {"kind": "youtube#video", "videoId": _vid(video)}}}
    r = _yt(write=True).playlistItems().insert(part="snippet", body=body).execute()
    return {"playlist_item_id": r["id"]}


def remove_from_playlist(playlist_item_id: str) -> str:
    """Remove an item from a playlist (playlist_item_id from get_playlist_items, not the video ID)."""
    _yt(write=True).playlistItems().delete(id=playlist_item_id).execute()
    return "Removed"


def update_video(video: str, title: str | None = None, description: str | None = None,
                 tags: list[str] | None = None, category_id: str | None = None,
                 privacy: str | None = None) -> dict:
    """Edit metadata of your own video. Only given fields change; others are kept."""
    yt = _yt(write=True)
    vid = _vid(video)
    cur = yt.videos().list(part="snippet,status", id=vid).execute()
    if not cur.get("items"):
        raise ValueError("Video not found")
    s, st = cur["items"][0]["snippet"], cur["items"][0]["status"]
    snippet = {"title": title or s["title"],
               "description": s.get("description", "") if description is None else description,
               "tags": s.get("tags", []) if tags is None else tags,
               "categoryId": category_id or s["categoryId"]}
    body = {"id": vid, "snippet": snippet}
    parts = "snippet"
    if privacy:
        body["status"] = {"privacyStatus": privacy, "embeddable": st.get("embeddable", True),
                          "license": st.get("license", "youtube"),
                          "publicStatsViewable": st.get("publicStatsViewable", True),
                          "selfDeclaredMadeForKids": st.get("selfDeclaredMadeForKids", False)}
        parts += ",status"
    yt.videos().update(part=parts, body=body).execute()
    return {"video_id": vid, "updated": [k for k, v in dict(title=title, description=description, tags=tags,
                                         category_id=category_id, privacy=privacy).items() if v is not None]}


def upload_video(file_path: str, title: str, description: str = "", tags: list[str] | None = None,
                 category_id: str = "22", privacy: str = "private", made_for_kids: bool = False) -> dict:
    """Upload a local video file. privacy defaults to private. category 22 = People & Blogs."""
    from googleapiclient.http import MediaFileUpload
    path = Path(file_path).expanduser()
    if not path.is_file():
        raise ValueError(f"File not found: {path}")
    body = {"snippet": {"title": title, "description": description, "tags": tags or [],
                        "categoryId": category_id},
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": made_for_kids}}
    media = MediaFileUpload(str(path), chunksize=8 * 1024 * 1024, resumable=True)
    req = _yt(write=True).videos().insert(part="snippet,status", body=body, media_body=media)
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    return {"video_id": resp["id"], "url": f"https://youtu.be/{resp['id']}",
            "privacy": resp["status"]["privacyStatus"]}


WRITE_TOOLS = [post_comment, reply_to_comment, rate_video, create_playlist,
               add_to_playlist, remove_from_playlist, update_video]

if not READ_ONLY:
    for _fn in WRITE_TOOLS:
        mcp.tool()(_fn)


# ---------- CLI ----------

def auth():
    from google_auth_oauthlib.flow import InstalledAppFlow
    if not CLIENT_SECRET.exists():
        sys.exit(f"Missing {CLIENT_SECRET}. Download OAuth client (Desktop app) JSON from Google Cloud Console.")
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES)
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    TOKEN.write_text(creds.to_json())
    print(f"Saved token to {TOKEN}")


def print_env():
    """Print the OAuth env vars to paste into Horizon (from token.json)."""
    if not TOKEN.exists():
        sys.exit("No token.json yet. Run: python server.py auth")
    c = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    if not c.refresh_token:
        sys.exit("token.json has no refresh token. Delete it and run: python server.py auth")
    print(f"YOUTUBE_CLIENT_ID={c.client_id}")
    print(f"YOUTUBE_CLIENT_SECRET={c.client_secret}")
    print(f"YOUTUBE_REFRESH_TOKEN={c.refresh_token}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "auth":
        auth()
    elif cmd == "print-env":
        print_env()
    else:
        # Local stdio run only: upload reads a file from this computer's disk,
        # which does not exist on a hosted server.
        if not READ_ONLY:
            mcp.tool()(upload_video)
        mcp.run()
