#!/usr/bin/env python3
"""Plex Cleanup — interactive TUI for finding unwatched Plex content."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

import requests
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button, Checkbox, DataTable, Footer, Header,
    Input, Label, LoadingIndicator, Select, Static, Switch,
)

# ── Constants ──────────────────────────────────────────────────────────────────

PLEX_SERVER    = "https://plex.plmr.cloud"
PLEX_TV_SIGNIN = "https://plex.tv/api/v2/users/signin"
CONFIG_FILE    = Path.home() / ".plex-cleanup.json"
CLIENT_ID      = "plex-cleanup-tool"

BASE_HEADERS = {
    "X-Plex-Client-Identifier": CLIENT_ID,
    "X-Plex-Product": "Plex Cleanup",
    "X-Plex-Version": "1.0",
    "Accept": "application/json",
}

ActivitySet = dict[str, dict[str, int]]
ArrApp = Literal["radarr", "sonarr"]

# ── Logging ────────────────────────────────────────────────────────────────────

LOG_FILE = Path(__file__).parent / "plex-cleanup.log"
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
_log = logging.getLogger("plex-cleanup")

# ── Config ─────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return {}

def save_config(cfg: dict) -> None:
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))

def get_saved_token() -> str | None:
    return load_config().get("token")

def save_token(token: str) -> None:
    cfg = load_config(); cfg["token"] = token; save_config(cfg)

def get_arr_cfg(app: ArrApp) -> dict | None:
    c = load_config().get(app)
    return c if (c and c.get("url") and c.get("api_key")) else None

def save_arr_cfg(app: ArrApp, url: str, api_key: str) -> None:
    cfg = load_config()
    cfg[app] = {"url": url.rstrip("/"), "api_key": api_key}
    save_config(cfg)

def get_skip_arr_prompt(app: ArrApp) -> bool:
    return bool(load_config().get(f"skip_arr_prompt_{app}", False))

def set_skip_arr_prompt(app: ArrApp, val: bool) -> None:
    cfg = load_config(); cfg[f"skip_arr_prompt_{app}"] = val; save_config(cfg)

def sign_in(username: str, password: str, otp: str = "") -> str:
    data: dict = {"login": username, "password": password}
    if otp:
        data["verificationCode"] = otp
    resp = requests.post(PLEX_TV_SIGNIN, headers=BASE_HEADERS, data=data, timeout=15)
    resp.raise_for_status()
    return resp.json()["authToken"]

# ── Plex API ───────────────────────────────────────────────────────────────────

def plex_get(path: str, token: str, **params) -> dict:
    resp = requests.get(
        f"{PLEX_SERVER}{path}",
        headers=BASE_HEADERS,
        params={"X-Plex-Token": token, **params},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["MediaContainer"]

def plex_delete(rk: str, token: str) -> None:
    requests.delete(
        f"{PLEX_SERVER}/library/metadata/{rk}",
        headers=BASE_HEADERS,
        params={"X-Plex-Token": token},
        timeout=30,
    ).raise_for_status()

def get_libraries(token: str) -> list[dict]:
    libs = plex_get("/library/sections", token).get("Directory", [])
    for lib in libs:
        try:
            mc = plex_get(
                f"/library/sections/{lib['key']}/all", token,
                **{"X-Plex-Container-Start": 0, "X-Plex-Container-Size": 0},
            )
            lib["count"] = mc.get("totalSize", mc.get("size", "?"))
        except Exception:
            lib["count"] = "?"
    return libs

def parse_guids(item: dict) -> dict[str, str]:
    """Return {provider: id} from Plex Guid array, e.g. {"tmdb": "123", "tvdb": "456"}."""
    return {
        g["id"].split("://")[0]: g["id"].split("://")[1]
        for g in item.get("Guid", [])
        if "://" in g.get("id", "")
    }

def _upd(m: dict, k: str, v: int) -> None:
    if k and (k not in m or v > m[k]):
        m[k] = v

def build_activity_set(token: str) -> ActivitySet:
    act: ActivitySet = {"movies": {}, "shows": {}, "episodes": {}}
    start = 0
    while True:
        c = plex_get(
            "/status/sessions/history/all", token,
            sort="viewedAt:desc",
            **{"X-Plex-Container-Start": start, "X-Plex-Container-Size": 1000},
        )
        items = c.get("Metadata", [])
        if not items:
            break
        for item in items:
            rk = str(item.get("ratingKey", ""))
            va = item.get("viewedAt", 0)
            t  = item.get("type", "")
            if t == "movie":
                _upd(act["movies"], rk, va)
            elif t == "episode":
                _upd(act["episodes"], rk, va)
                gprk = item.get("grandparentRatingKey") or \
                    item.get("grandparentKey", "").rstrip("/").rsplit("/", 1)[-1]
                _upd(act["shows"], str(gprk) if gprk else "", va)
        total = int(c.get("totalSize", c.get("size", len(items))))
        start += len(items)
        if start >= total:
            break
    return act

# ── Arr API ────────────────────────────────────────────────────────────────────

def _arr_req(method: str, cfg: dict, path: str, **kwargs) -> any:
    hdrs = {
        "X-Api-Key": cfg["api_key"],
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    resp = requests.request(method, f"{cfg['url']}{path}", headers=hdrs, timeout=30, **kwargs)
    resp.raise_for_status()
    return resp.json() if resp.content else None

def test_arr_connection(app: ArrApp, url: str, api_key: str) -> str:
    """Returns instance name on success, raises on failure."""
    cfg = {"url": url.rstrip("/"), "api_key": api_key}
    data = _arr_req("GET", cfg, "/api/v3/system/status")
    return data.get("instanceName") or data.get("appName") or app.title()

def radarr_find(cfg: dict, tmdb_id: str) -> dict | None:
    for m in (_arr_req("GET", cfg, "/api/v3/movie") or []):
        if str(m.get("tmdbId", "")) == str(tmdb_id):
            return m
    return None

def radarr_delete(cfg: dict, movie_id: int, delete_files: bool) -> None:
    _arr_req("DELETE", cfg, f"/api/v3/movie/{movie_id}",
             params={"deleteFiles": str(delete_files).lower()})

def sonarr_find(cfg: dict, tvdb_id: str) -> dict | None:
    for s in (_arr_req("GET", cfg, "/api/v3/series") or []):
        if str(s.get("tvdbId", "")) == str(tvdb_id):
            return s
    return None

def sonarr_delete_series(cfg: dict, series_id: int, delete_files: bool) -> None:
    _arr_req("DELETE", cfg, f"/api/v3/series/{series_id}",
             params={"deleteFiles": str(delete_files).lower()})

def sonarr_delete_season(cfg: dict, series_id: int, season_num: int) -> None:
    """Delete episode files for one season and unmonitor it."""
    files = _arr_req("GET", cfg, "/api/v3/episodefile",
                     params={"seriesId": series_id, "seasonNumber": season_num}) or []
    if files:
        _arr_req("DELETE", cfg, "/api/v3/episodefile/bulk",
                 json={"episodeFileIds": [f["id"] for f in files]})
    # Unmonitor the season so it won't be re-queued
    series = _arr_req("GET", cfg, f"/api/v3/series/{series_id}")
    for s in series.get("seasons", []):
        if s["seasonNumber"] == season_num:
            s["monitored"] = False
            break
    _arr_req("PUT", cfg, f"/api/v3/series/{series_id}", json=series)

# ── Formatting ─────────────────────────────────────────────────────────────────

def fmt_size(n: int) -> str:
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} PB"

_RATING_SRC_MAP = {
    "rottentomatoes": "RT",
    "imdb":           "IMDb",
    "themoviedb":     "TMDB",
    "tvdb":           "TVDB",
}

def _rating_src(img: str) -> str:
    for k, v in _RATING_SRC_MAP.items():
        if k in img:
            return v
    return ""

def fmt_rating(val: float | None, img: str) -> str:
    if val is None:
        return "—"
    src = _rating_src(img)
    return f"{val:.1f}" + (f" {src}" if src else "")

def fmt_date(ts: int | None) -> str:
    return "Never" if not ts else datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d")

def fmt_age(ts: int | None) -> str:
    if not ts:
        return "unknown"
    d = (datetime.now() - datetime.fromtimestamp(int(ts))).days
    if d < 1:   return "today"
    if d < 30:  return f"{d}d ago"
    m = d // 30
    if m < 12:  return f"{m}mo ago"
    y, r = divmod(m, 12)
    return f"{y}y {r}mo ago" if r else f"{y}y ago"

def is_stale(rk: str, act: dict, cut: int | None, never: bool) -> bool:
    v = act.get(rk)
    if never:
        return v is None
    return v is None or v < cut  # type: ignore[operator]

# ── Scan ───────────────────────────────────────────────────────────────────────

def scan_movies(
    token: str, key: str, act: ActivitySet, cut: int | None, never: bool,
) -> list[dict]:
    out = []
    for item in plex_get(f"/library/sections/{key}/all", token, includeGuids=1).get("Metadata", []):
        rk = str(item["ratingKey"])
        if not is_stale(rk, act["movies"], cut, never):
            continue
        size = sum(
            p.get("size", 0)
            for m in item.get("Media", [])
            for p in m.get("Part", [])
        )
        out.append({
            "rk": rk, "type": "movie",
            "title": item["title"], "year": item.get("year", ""),
            "last_viewed_ts": act["movies"].get(rk),
            "added_ts": item.get("addedAt"),
            "size": size,
            "guids": parse_guids(item),
            "rating":           item.get("rating"),
            "rating_img":       item.get("ratingImage", ""),
            "audience_rating":  item.get("audienceRating"),
            "audience_img":     item.get("audienceRatingImage", ""),
        })
    return out

FULLY_WATCHED = "Fully Watched"
IN_PROGRESS   = "In Progress"
NEVER_WATCHED = "Never Watched"

def _assign_safety(seasons: list[dict]) -> None:
    in_prog = any(s["status"] == IN_PROGRESS for s in seasons)
    for s in seasons:
        if s["status"] == FULLY_WATCHED:
            s["safe_to_remove"] = True;  s["action"] = "REMOVE"
        elif s["status"] == IN_PROGRESS:
            s["safe_to_remove"] = False; s["action"] = "KEEP"
        else:
            s["safe_to_remove"] = not in_prog
            s["action"] = "PENDING" if in_prog else "REMOVE"

def scan_shows(
    token: str, key: str, act: ActivitySet, cut: int | None, never: bool,
) -> list[dict]:
    stale: dict[str, dict] = {}
    for show in plex_get(f"/library/sections/{key}/all", token, includeGuids=1).get("Metadata", []):
        rk = str(show["ratingKey"])
        if not is_stale(rk, act["shows"], cut, never):
            continue
        stale[rk] = {
            "rk": rk, "type": "show",
            "title": show["title"], "year": show.get("year", ""),
            "last_viewed_ts": act["shows"].get(rk),
            "added_ts": show.get("addedAt"),
            "total_size": 0, "safe_size": 0, "seasons": {},
            "guids": parse_guids(show),
            "rating":           show.get("rating"),
            "rating_img":       show.get("ratingImage", ""),
            "audience_rating":  show.get("audienceRating"),
            "audience_img":     show.get("audienceRatingImage", ""),
        }
    if not stale:
        return []
    for ep in plex_get(f"/library/sections/{key}/all", token, type=4).get("Metadata", []):
        gk = str(ep.get("grandparentRatingKey", ""))
        if gk not in stale:
            continue
        rk = str(ep.get("ratingKey", ""))
        sn = ep.get("parentIndex", 0)
        lv = act["episodes"].get(rk)
        w  = rk in act["episodes"]
        sz = sum(p.get("size", 0) for m in ep.get("Media", []) for p in m.get("Part", []))
        sh = stale[gk]
        sh["total_size"] += sz
        if sn not in sh["seasons"]:
            sh["seasons"][sn] = {
                "number": sn, "total_episodes": 0,
                "watched_episodes": 0, "size": 0, "last_viewed_ts": None,
            }
        s = sh["seasons"][sn]
        s["total_episodes"] += 1
        s["size"] += sz
        if w:
            s["watched_episodes"] += 1
            if lv and (s["last_viewed_ts"] is None or lv > s["last_viewed_ts"]):
                s["last_viewed_ts"] = lv
    for sh in stale.values():
        sl = sorted(sh["seasons"].values(), key=lambda s: s["number"])
        for s in sl:
            w, t = s["watched_episodes"], s["total_episodes"]
            s["status"] = NEVER_WATCHED if w == 0 else (FULLY_WATCHED if w >= t else IN_PROGRESS)
            s["last_viewed"] = fmt_date(s["last_viewed_ts"])
        _assign_safety(sl)
        sh["seasons"]  = sl
        sh["safe_size"] = sum(s["size"] for s in sl if s["safe_to_remove"])
        season_max = max((s["last_viewed_ts"] for s in sl if s["last_viewed_ts"]), default=None)
        if season_max and (not sh["last_viewed_ts"] or season_max > sh["last_viewed_ts"]):
            sh["last_viewed_ts"] = season_max

    out = []
    for sh in stale.values():
        lv = sh["last_viewed_ts"]
        if never:
            if lv is None:
                out.append(sh)
        else:
            if lv is None or lv < cut:  # type: ignore[operator]
                out.append(sh)
    return out

# ── Sort / filter ──────────────────────────────────────────────────────────────

SORT_OPTIONS: list[tuple[str, str]] = [
    ("Size ↓  (largest first)",      "size_desc"),
    ("Size ↑  (smallest first)",     "size_asc"),
    ("Last Watched  oldest first",   "lw_asc"),
    ("Last Watched  most recent",    "lw_desc"),
    ("Date Added  oldest first",     "added_asc"),
    ("Date Added  newest first",     "added_desc"),
    ("Rating ↓  (highest first)",    "rating_desc"),
    ("Rating ↑  (lowest first)",     "rating_asc"),
    ("Title  A → Z",                 "title_asc"),
    ("Title  Z → A",                 "title_desc"),
]

RATING_OPTIONS: list[tuple[str, str]] = [
    ("Critic",   "critic"),
    ("Audience", "audience"),
]

@dataclass
class FilterState:
    days:       int  = 365
    never_only: bool = False
    safe_only:  bool = False
    sort:       str  = "size_desc"
    rating_src: str  = "critic"

def apply_sort(items: list[dict], state: FilterState) -> list[dict]:
    if state.safe_only:
        items = [i for i in items if i.get("safe_size", i.get("size", 0)) > 0]
    s   = state.sort
    src = state.rating_src

    def key(i: dict):
        if s.startswith("size"):   return i.get("total_size", i.get("size", 0))
        if s.startswith("lw"):     return i.get("last_viewed_ts") or 0
        if s.startswith("added"):  return i.get("added_ts") or 0
        if s.startswith("rating"):
            field = "rating" if src == "critic" else "audience_rating"
            return i.get(field) or 0.0
        return i.get("title", "").lower()

    reverse = s.endswith("_desc")
    if s.startswith(("lw", "added")):
        has_v = sorted([i for i in items if key(i)], key=key, reverse=reverse)
        no_v  = [i for i in items if not key(i)]
        return has_v + no_v
    if s.startswith("rating"):
        field = "rating" if src == "critic" else "audience_rating"
        has_v = sorted([i for i in items if i.get(field) is not None], key=key, reverse=reverse)
        no_v  = [i for i in items if i.get(field) is None]
        return has_v + no_v
    return sorted(items, key=key, reverse=reverse)

# ── Modals ─────────────────────────────────────────────────────────────────────

class DeleteModal(ModalScreen):
    """Delete confirmation with optional Radarr/Sonarr integration."""

    CSS = """
    DeleteModal { align: center middle; }
    #dm-panel {
        width: 68; padding: 2 3;
        background: $surface; border: round $error;
        height: auto;
    }
    #dm-title  { text-style: bold; color: $error; margin-bottom: 1; }
    #dm-subject { margin-bottom: 1; }
    #guard-msg {
        color: $warning; margin-bottom: 1;
        border: solid $warning; padding: 0 1;
    }
    #arr-title { text-style: bold; margin-top: 1; margin-bottom: 0; }
    DeleteModal Checkbox { margin-bottom: 0; }
    #dm-btns { margin-top: 1; height: auto; }
    #dm-btns Button { margin-right: 1; }
    #btn-arr { margin-bottom: 1; }
    """

    def __init__(self, item: dict, season: dict | None = None) -> None:
        super().__init__()
        self.item    = item
        self.season  = season
        self._arr_app: ArrApp = "sonarr" if item["type"] == "show" else "radarr"
        self._arr_cfg = get_arr_cfg(self._arr_app)
        self._skip    = get_skip_arr_prompt(self._arr_app)
        self._show_guard = not self._arr_cfg and not self._skip

    def compose(self) -> ComposeResult:
        arr_name = self._arr_app.title()
        if self.season:
            sn = self.season["number"]
            label = "Specials" if sn == 0 else f"Season {sn}"
            subject = f"{self.item['title']} — {label}  ({fmt_size(self.season['size'])})"
        else:
            sz = self.item.get("total_size", self.item.get("size", 0))
            subject = f"{self.item['title']}  ({fmt_size(sz)})"

        with Container(id="dm-panel"):
            yield Static("Confirm Delete", id="dm-title")
            yield Static(subject, id="dm-subject")

            if self._show_guard:
                yield Static(
                    f"{arr_name} is not configured. Without it, deleted content "
                    f"may be re-downloaded automatically.",
                    id="guard-msg",
                )
                yield Checkbox("Don't show this again  (always delete from Plex only)", id="cb-skip")

            if self._arr_cfg:
                yield Static(f"Delete via {arr_name}  (recommended)", id="arr-title")
                yield Checkbox("Delete files from disk", id="cb-delete-files", value=True)
                with Horizontal(id="dm-btns"):
                    yield Button(f"Delete via {arr_name}", id="btn-arr", variant="error")
                    yield Button("Delete from Plex only", id="btn-plex", variant="warning")
                    yield Button("Cancel", id="btn-cancel")
            else:
                with Horizontal(id="dm-btns"):
                    if self._show_guard:
                        yield Button(f"Configure {arr_name}", id="btn-configure", variant="primary")
                    yield Button("Delete from Plex only", id="btn-plex", variant="warning")
                    yield Button("Cancel", id="btn-cancel")

    def _read_skip_cb(self) -> bool:
        try:
            return self.query_one("#cb-skip", Checkbox).value
        except Exception:
            return False

    @on(Button.Pressed, "#btn-configure")
    def do_configure(self) -> None:
        if self._read_skip_cb():
            set_skip_arr_prompt(self._arr_app, True)
        self.dismiss("configure")

    @on(Button.Pressed, "#btn-plex")
    def do_plex(self) -> None:
        if self._read_skip_cb():
            set_skip_arr_prompt(self._arr_app, True)
        self.dismiss({"method": "plex"})

    @on(Button.Pressed, "#btn-arr")
    def do_arr(self) -> None:
        delete_files = self.query_one("#cb-delete-files", Checkbox).value
        self.dismiss({"method": "arr", "delete_files": delete_files})

    @on(Button.Pressed, "#btn-cancel")
    def do_cancel(self) -> None:
        self.dismiss(None)


# ── Arr Config Screen ──────────────────────────────────────────────────────────

class ArrConfigScreen(Screen):
    """Configure Radarr or Sonarr connection."""

    BINDINGS = [Binding("escape", "go_back", "Back")]
    CSS = """
    ArrConfigScreen { align: center middle; }
    #cfg-panel {
        width: 64; padding: 2 4;
        background: $surface; border: round $primary;
        height: auto;
    }
    #cfg-title { text-style: bold; color: $accent; text-align: center; margin-bottom: 1; }
    ArrConfigScreen Label { color: $text-muted; margin-bottom: 0; }
    ArrConfigScreen Input { margin-bottom: 1; }
    #cfg-status { height: 1; margin-top: 0; }
    #cfg-btns { margin-top: 1; height: auto; }
    #cfg-btns Button { margin-right: 1; }
    """

    # ── Messages ───────────────────────────────────────────────────────────────
    class TestResult(Message):
        def __init__(self, ok: bool, text: str) -> None:
            self.ok   = ok
            self.text = text
            super().__init__()

    def __init__(self, app: ArrApp) -> None:
        super().__init__()
        self._app = app

    def compose(self) -> ComposeResult:
        name = self._app.title()
        existing = get_arr_cfg(self._app) or {}
        with Container(id="cfg-panel"):
            yield Label(f"Configure {name}", id="cfg-title")
            yield Label("Base URL  (e.g. http://localhost:7878)")
            yield Input(existing.get("url", ""), placeholder=f"http://localhost:{'7878' if self._app == 'radarr' else '8989'}", id="cfg-url")
            yield Label("API Key")
            yield Input(existing.get("api_key", ""), placeholder="Paste API key from Settings → General", id="cfg-key", password=True)
            yield Static("", id="cfg-status")
            with Horizontal(id="cfg-btns"):
                yield Button("Test Connection", id="btn-test", variant="default")
                yield Button("Save", id="btn-save", variant="primary")
                yield Button("Back", id="btn-back")
        yield Footer()

    @on(Button.Pressed, "#btn-test")
    def do_test(self) -> None:
        url = self.query_one("#cfg-url", Input).value.strip()
        key = self.query_one("#cfg-key", Input).value.strip()
        if not url or not key:
            self.query_one("#cfg-status", Static).update("[red]URL and API key are required.[/red]")
            return
        self.query_one("#cfg-status", Static).update("Testing…")
        self._test_worker(url, key)

    @work(thread=True)
    def _test_worker(self, url: str, key: str) -> None:
        try:
            name = test_arr_connection(self._app, url, key)
            self.post_message(ArrConfigScreen.TestResult(True, f"Connected: {name}"))
        except Exception as e:
            self.post_message(ArrConfigScreen.TestResult(False, f"Failed: {e}"))

    def on_arr_config_screen_test_result(self, event: TestResult) -> None:
        color = "green" if event.ok else "red"
        self.query_one("#cfg-status", Static).update(f"[{color}]{event.text}[/{color}]")

    @on(Button.Pressed, "#btn-save")
    def do_save(self) -> None:
        url = self.query_one("#cfg-url", Input).value.strip()
        key = self.query_one("#cfg-key", Input).value.strip()
        if not url or not key:
            self.query_one("#cfg-status", Static).update("[red]URL and API key are required.[/red]")
            return
        save_arr_cfg(self._app, url, key)
        self.notify(f"{self._app.title()} configuration saved.", severity="information")
        self.app.pop_screen()

    @on(Button.Pressed, "#btn-back")
    def action_go_back(self) -> None:
        self.app.pop_screen()


# ── Settings Screen ────────────────────────────────────────────────────────────

class SettingsScreen(Screen):
    """Overview of arr integrations with links to configure each."""

    BINDINGS = [Binding("escape,q", "go_back", "Back")]
    CSS = """
    SettingsScreen { align: center middle; }
    #set-panel {
        width: 64; padding: 2 4;
        background: $surface; border: round $primary;
        height: auto;
    }
    #set-title { text-style: bold; color: $accent; text-align: center; margin-bottom: 1; }
    .set-row { height: 3; align: left middle; margin-bottom: 1; }
    .set-label { width: 12; color: $text-muted; }
    .set-status { width: 1fr; }
    .set-status.ok { color: $success; }
    .set-status.missing { color: $warning; }
    """

    def compose(self) -> ComposeResult:
        radarr = get_arr_cfg("radarr")
        sonarr = get_arr_cfg("sonarr")
        with Container(id="set-panel"):
            yield Static("Integrations", id="set-title")
            with Horizontal(classes="set-row"):
                yield Static("Radarr", classes="set-label")
                if radarr:
                    yield Static(f"Configured  ({radarr['url']})", classes="set-status ok")
                else:
                    yield Static("Not configured", classes="set-status missing")
                yield Button("Edit", id="btn-radarr", variant="default")
            with Horizontal(classes="set-row"):
                yield Static("Sonarr", classes="set-label")
                if sonarr:
                    yield Static(f"Configured  ({sonarr['url']})", classes="set-status ok")
                else:
                    yield Static("Not configured", classes="set-status missing")
                yield Button("Edit", id="btn-sonarr", variant="default")
            yield Button("Back", id="btn-back", variant="default")
        yield Footer()

    @on(Button.Pressed, "#btn-radarr")
    def edit_radarr(self) -> None:
        self.app.push_screen(ArrConfigScreen("radarr"))

    @on(Button.Pressed, "#btn-sonarr")
    def edit_sonarr(self) -> None:
        self.app.push_screen(ArrConfigScreen("sonarr"))

    @on(Button.Pressed, "#btn-back")
    def action_go_back(self) -> None:
        self.app.pop_screen()


# ── Auth Screen ────────────────────────────────────────────────────────────────

class AuthScreen(Screen):
    class SignedIn(Message):
        def __init__(self, token: str) -> None:
            self.token = token; super().__init__()

    class SignInFailed(Message):
        def __init__(self, error: str) -> None:
            self.error = error; super().__init__()

    CSS = """
    AuthScreen { align: center middle; }
    #panel {
        width: 58; padding: 2 4;
        background: $surface; border: round $primary;
    }
    #ttl  { text-align: center; text-style: bold; color: $accent; margin-bottom: 1; }
    AuthScreen Label { color: $text-muted; margin-bottom: 0; }
    AuthScreen Input { margin-bottom: 1; }
    #btn  { width: 100%; margin-top: 1; }
    #err  { color: $error; text-align: center; height: 1; }
    """

    def compose(self) -> ComposeResult:
        with Container(id="panel"):
            yield Label("PLEX CLEANUP", id="ttl")
            yield Label("Sign in with your Plex.tv account")
            yield Input(placeholder="Email / Username", id="username")
            yield Input(placeholder="Password", password=True, id="password")
            yield Input(placeholder="Two-factor code  (leave blank if not required)", id="otp")
            yield Button("Sign In", variant="primary", id="btn")
            yield Static("", id="err")

    @on(Button.Pressed, "#btn")
    def on_sign_in(self) -> None:
        self._attempt_sign_in()

    @on(Input.Submitted)
    def on_submit(self) -> None:
        self._attempt_sign_in()

    def _attempt_sign_in(self) -> None:
        u = self.query_one("#username", Input).value.strip()
        p = self.query_one("#password", Input).value
        o = self.query_one("#otp", Input).value.strip()
        if not u or not p:
            self.query_one("#err", Static).update("Username and password are required.")
            return
        self.query_one("#btn", Button).disabled = True
        self.query_one("#err", Static).update("Signing in…")
        self._sign_in_worker(u, p, o)

    @work(thread=True)
    def _sign_in_worker(self, u: str, p: str, o: str) -> None:
        try:
            token = sign_in(u, p, o)
            save_token(token)
            self.post_message(AuthScreen.SignedIn(token))
        except requests.HTTPError as e:
            msg = "Invalid credentials." if (e.response and e.response.status_code == 401) else f"Sign-in error: {e}"
            self.post_message(AuthScreen.SignInFailed(msg))
        except Exception as e:
            self.post_message(AuthScreen.SignInFailed(f"Error: {e}"))

    def on_auth_screen_signed_in(self, event: SignedIn) -> None:
        self.app.token = event.token  # type: ignore[attr-defined]
        self.app.switch_screen(LibraryScreen())

    def on_auth_screen_sign_in_failed(self, event: SignInFailed) -> None:
        self.query_one("#err", Static).update(event.error)
        self.query_one("#btn", Button).disabled = False


# ── Library Screen ─────────────────────────────────────────────────────────────

class LibraryScreen(Screen):
    class LibsReady(Message):
        def __init__(self, libs: list[dict]) -> None:
            self.libs = libs; super().__init__()

    class ActivityStatus(Message):
        def __init__(self, text: str) -> None:
            self.text = text; super().__init__()

    class ActivityReady(Message):
        def __init__(self, act: ActivitySet) -> None:
            self.act = act; super().__init__()

    BINDINGS = [
        Binding("q", "app.quit", "Quit"),
        Binding("s", "open_settings", "Settings"),
    ]
    CSS = """
    LibraryScreen { align: center middle; }
    #panel {
        width: 72; padding: 1 2;
        background: $surface; border: round $primary;
        height: auto; max-height: 90%;
    }
    #ttl  { text-align: center; text-style: bold; color: $accent; margin-bottom: 1; }
    #act  { text-align: center; color: $text-muted; height: 1; margin-top: 1; }
    #libs { height: auto; max-height: 20; }
    """

    def __init__(self) -> None:
        super().__init__()
        self._libs: dict[str, dict] = {}

    def compose(self) -> ComposeResult:
        with Container(id="panel"):
            yield Label("PLEX CLEANUP", id="ttl")
            yield LoadingIndicator(id="loading")
            yield DataTable(id="libs", show_cursor=True)
            yield Static("", id="act")
        yield Footer()

    def on_mount(self) -> None:
        t = self.query_one("#libs", DataTable)
        t.cursor_type = "row"
        t.add_columns("TYPE", "LIBRARY", "ITEMS")
        self._load_libraries()
        if self.app.activity is None:  # type: ignore[attr-defined]
            self._load_activity()
        else:
            self._show_act_loaded()

    @work(thread=True)
    def _load_libraries(self) -> None:
        libs = get_libraries(self.app.token)  # type: ignore[attr-defined]
        self.post_message(LibraryScreen.LibsReady(libs))

    def on_library_screen_libs_ready(self, event: LibsReady) -> None:
        try:
            self.query_one("#loading").remove()
        except Exception:
            pass
        t = self.query_one("#libs", DataTable)
        for lib in event.libs:
            kind  = "Movie" if lib["type"] == "movie" else "TV"
            count = str(lib.get("count", "?"))
            t.add_row(kind, lib["title"], count, key=lib["key"])
            self._libs[lib["key"]] = lib

    @work(thread=True)
    def _load_activity(self) -> None:
        self.post_message(LibraryScreen.ActivityStatus("Loading watch history for all users…"))
        try:
            act = build_activity_set(self.app.token)  # type: ignore[attr-defined]
            self.app.activity = act  # type: ignore[attr-defined]
            self.post_message(LibraryScreen.ActivityReady(act))
        except Exception as e:
            self.post_message(LibraryScreen.ActivityStatus(f"History load failed: {e}"))

    def on_library_screen_activity_status(self, event: ActivityStatus) -> None:
        self.query_one("#act", Static).update(event.text)

    def on_library_screen_activity_ready(self, event: ActivityReady) -> None:
        self._show_act_loaded()

    def _show_act_loaded(self) -> None:
        act = self.app.activity  # type: ignore[attr-defined]
        if act:
            self.query_one("#act", Static).update(
                f"History loaded · {len(act['movies'])} movies · {len(act['shows'])} shows across all users"
            )

    @on(DataTable.RowSelected, "#libs")
    def on_lib_selected(self, event: DataTable.RowSelected) -> None:
        lib = self._libs.get(str(event.row_key.value))
        if not lib:
            return
        if self.app.activity is None:  # type: ignore[attr-defined]
            self.notify("Watch history is still loading — please wait.", severity="warning")
            return
        self.app.push_screen(GridScreen(lib))

    def action_open_settings(self) -> None:
        self.app.push_screen(SettingsScreen())


# ── Grid Screen ────────────────────────────────────────────────────────────────

class GridScreen(Screen):
    BINDINGS = [
        Binding("escape", "go_back", "Back"),
        Binding("r", "refresh_data", "Refresh"),
        Binding("d", "delete_item", "Delete"),
    ]
    CSS = """
    GridScreen { layout: vertical; }
    #filter-bar {
        height: auto; dock: top;
        background: $surface;
        border-bottom: solid $surface-lighten-2;
        padding: 0 2;
        align: left middle;
    }
    #filter-bar Label { color: $text-muted; height: 1; padding: 0; margin: 0 1; }
    #days-input       { width: 7; height: 1; border: none; padding: 0 1;
                        background: $surface-lighten-1; margin: 0 1; }
    #days-input:focus { background: $surface-lighten-2; }
    #never-sw                    { height: 1; border: none; margin: 0 1; }
    #safe-sw                     { height: 1; border: none; margin: 0 1; }
    #rating-src                  { width: 12; height: 1; border: none; margin: 0 1; }
    #rating-src SelectCurrent    { height: 1; border: none; padding: 0 1; }
    #sort-sel                    { width: 22; height: 1; border: none; margin: 0 1; }
    #sort-sel SelectCurrent      { height: 1; border: none; padding: 0 1; }
    #apply-btn        { min-width: 7; height: 1; border: none; padding: 0 2; margin: 0 1; }
    #total-lbl        { width: 1fr; text-align: right; height: 1;
                        color: $accent; text-style: bold; }
    #grid             { height: 1fr; }
    #status           {
        height: 1; dock: bottom;
        background: $surface; padding: 0 1; color: $text-muted;
        border-top: solid $surface-lighten-2;
    }
    """

    # ── Messages ───────────────────────────────────────────────────────────────
    class ItemsReady(Message):
        def __init__(self, items: list[dict]) -> None:
            self.items = items; super().__init__()

    class FetchStatus(Message):
        def __init__(self, text: str) -> None:
            self.text = text; super().__init__()

    class DeleteStatus(Message):
        def __init__(self, text: str, ok: bool = True, rk: str = "") -> None:
            self.text = text; self.ok = ok; self.rk = rk; super().__init__()

    def __init__(self, library: dict) -> None:
        super().__init__()
        self.library = library
        self._filter = FilterState()
        self._items: list[dict] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal(id="filter-bar"):
            yield Label("Days:")
            yield Input(str(self._filter.days), id="days-input")
            yield Label("Never only")
            yield Switch(False, id="never-sw")
            yield Label("Safe only")
            yield Switch(False, id="safe-sw")
            yield Label("Rating:")
            yield Select(RATING_OPTIONS, value="critic", id="rating-src")
            yield Label("Sort:")
            yield Select(SORT_OPTIONS, value="size_desc", id="sort-sel")
            yield Button("Apply", variant="primary", id="apply-btn")
            yield Static("", id="total-lbl")
        yield DataTable(id="grid", show_cursor=True)
        yield Static("Loading…", id="status")
        yield Footer()

    def on_mount(self) -> None:
        self.app.sub_title = self.library["title"]
        t = self.query_one("#grid", DataTable)
        t.cursor_type = "row"
        self._setup_columns()
        self._fetch()

    def on_unmount(self) -> None:
        self.app.sub_title = ""

    def _setup_columns(self) -> None:
        t = self.query_one("#grid", DataTable)
        t.clear(columns=True)
        if self.library["type"] == "movie":
            t.add_columns("TITLE", "YEAR", "LAST WATCHED", "IN LIBRARY", "SIZE", "RATING")
        else:
            t.add_columns("TITLE", "YEAR", "LAST WATCHED", "IN LIBRARY", "TOTAL", "SAFE", "STATUS", "RATING")

    def _refresh_table(self) -> None:
        t = self.query_one("#grid", DataTable)
        # Preserve cursor position across re-renders
        prior_rows = list(t.ordered_rows)
        anchor_rk  = str(prior_rows[t.cursor_row].key.value) if prior_rows and t.cursor_row >= 0 else None
        t.clear()
        items = apply_sort(self._items, self._filter)
        is_movie = self.library["type"] == "movie"
        src = self._filter.rating_src
        for item in items:
            title  = f"{item['title']} ({item.get('year', '')})"
            lw     = fmt_date(item.get("last_viewed_ts"))
            added  = fmt_age(item.get("added_ts"))
            if src == "critic":
                rating = fmt_rating(item.get("rating"), item.get("rating_img", ""))
            else:
                rating = fmt_rating(item.get("audience_rating"), item.get("audience_img", ""))
            if is_movie:
                t.add_row(title, str(item.get("year", "")), lw, added,
                          fmt_size(item.get("size", 0)), rating, key=item["rk"])
            else:
                seasons = item.get("seasons", [])
                if any(s["status"] == IN_PROGRESS for s in seasons):
                    status = "In Progress"
                elif all(s["status"] == NEVER_WATCHED for s in seasons):
                    status = "Unwatched"
                elif all(s["status"] == FULLY_WATCHED for s in seasons):
                    status = "All Watched"
                else:
                    status = "Mixed"
                t.add_row(title, str(item.get("year", "")), lw, added,
                          fmt_size(item.get("total_size", 0)),
                          fmt_size(item.get("safe_size", 0)),
                          status, rating, key=item["rk"])

        # Restore cursor to the same item, or the nearest row if it was deleted
        if anchor_rk is not None:
            rk_list = [i["rk"] for i in items]
            if anchor_rk in rk_list:
                t.move_cursor(row=rk_list.index(anchor_rk), animate=False)
            elif rk_list:
                t.move_cursor(row=min(t.cursor_row, len(rk_list) - 1), animate=False)

        total = sum(i.get("total_size", i.get("size", 0)) for i in items)
        safe  = sum(i.get("safe_size",  i.get("size", 0)) for i in items)
        label = "never watched" if self._filter.never_only else f"{self._filter.days}d inactive"
        self.query_one("#total-lbl", Static).update(
            f"{len(items)} items · {fmt_size(total)} flagged · {fmt_size(safe)} safe"
        )
        self.query_one("#status", Static).update(f"Filter: {label}")

    # ── Workers ────────────────────────────────────────────────────────────────

    @work(thread=True)
    def _fetch(self) -> None:
        self.post_message(GridScreen.FetchStatus("Fetching…"))
        f   = self._filter
        cut = None if f.never_only else int(
            (datetime.now() - timedelta(days=f.days)).timestamp()
        )
        try:
            act = self.app.activity  # type: ignore[attr-defined]
            if self.library["type"] == "movie":
                items = scan_movies(self.app.token, self.library["key"], act, cut, f.never_only)  # type: ignore[attr-defined]
            else:
                items = scan_shows(self.app.token, self.library["key"], act, cut, f.never_only)  # type: ignore[attr-defined]
            self.post_message(GridScreen.ItemsReady(items))
        except Exception as e:
            self.post_message(GridScreen.FetchStatus(f"Error: {e}"))

    @work(thread=True)
    def _do_delete(self, item: dict, method: str, delete_files: bool) -> None:
        title = item["title"]
        arr_app: ArrApp = "sonarr" if item["type"] == "show" else "radarr"
        try:
            if method == "arr":
                cfg = get_arr_cfg(arr_app)
                if not cfg:
                    raise RuntimeError(f"{arr_app.title()} is not configured.")
                guids = item.get("guids", {})
                if arr_app == "radarr":
                    tmdb = guids.get("tmdb")
                    if not tmdb:
                        raise RuntimeError("No TMDB ID found for this movie.")
                    rec = radarr_find(cfg, tmdb)
                    if not rec:
                        raise RuntimeError(f"Movie not found in Radarr (TMDB {tmdb}).")
                    radarr_delete(cfg, rec["id"], delete_files)
                    _log.info("DELETED movie %r via Radarr (files_deleted=%s)", title, delete_files)
                else:
                    tvdb = guids.get("tvdb")
                    if not tvdb:
                        raise RuntimeError("No TVDB ID found for this show.")
                    rec = sonarr_find(cfg, tvdb)
                    if not rec:
                        raise RuntimeError(f"Show not found in Sonarr (TVDB {tvdb}).")
                    sonarr_delete_series(cfg, rec["id"], delete_files)
                    _log.info("DELETED show %r via Sonarr (files_deleted=%s)", title, delete_files)
                self.post_message(GridScreen.DeleteStatus(f"Deleted '{title}' via {arr_app.title()}.", rk=item["rk"]))
            else:
                plex_delete(item["rk"], self.app.token)  # type: ignore[attr-defined]
                _log.info("DELETED %s %r from Plex library (files on disk unchanged)", item["type"], title)
                self.post_message(GridScreen.DeleteStatus(f"Deleted '{title}' from Plex.", rk=item["rk"]))
        except Exception as e:
            _log.error("DELETE FAILED for %r: %s", title, e)
            self.post_message(GridScreen.DeleteStatus(f"Delete failed: {e}", ok=False))

    # ── Message handlers ───────────────────────────────────────────────────────

    def on_grid_screen_fetch_status(self, event: FetchStatus) -> None:
        self.query_one("#status", Static).update(event.text)

    def on_grid_screen_items_ready(self, event: ItemsReady) -> None:
        self._items = event.items
        self._refresh_table()

    def on_grid_screen_delete_status(self, event: DeleteStatus) -> None:
        sev = "information" if event.ok else "error"
        self.notify(event.text, severity=sev)
        if event.ok and event.rk:
            self._items = [i for i in self._items if i["rk"] != event.rk]
            self._refresh_table()

    # ── Filter interactions ────────────────────────────────────────────────────

    @on(Button.Pressed, "#apply-btn")
    def apply(self) -> None:
        days_val   = self.query_one("#days-input", Input).value.strip()
        never      = self.query_one("#never-sw", Switch).value
        safe       = self.query_one("#safe-sw",  Switch).value
        sort_val   = self.query_one("#sort-sel", Select).value
        rating_val = self.query_one("#rating-src", Select).value

        new_days    = int(days_val) if days_val.isdigit() else self._filter.days
        needs_fetch = never != self._filter.never_only or new_days != self._filter.days

        self._filter.days       = new_days
        self._filter.never_only = never
        self._filter.safe_only  = safe
        if sort_val is not Select.BLANK:
            self._filter.sort = str(sort_val)
        if rating_val is not Select.BLANK:
            self._filter.rating_src = str(rating_val)

        if needs_fetch:
            self._fetch()
        else:
            self._refresh_table()

    @on(Select.Changed, "#sort-sel")
    def on_sort_changed(self, event: Select.Changed) -> None:
        if event.value is not Select.BLANK:
            self._filter.sort = str(event.value)
            if self._items:
                self._refresh_table()

    @on(Select.Changed, "#rating-src")
    def on_rating_src_changed(self, event: Select.Changed) -> None:
        if event.value is not Select.BLANK:
            self._filter.rating_src = str(event.value)
            if self._items:
                self._refresh_table()

    @on(Switch.Changed, "#safe-sw")
    def on_safe_changed(self, event: Switch.Changed) -> None:
        self._filter.safe_only = event.value
        if self._items:
            self._refresh_table()

    # ── Navigation / delete ────────────────────────────────────────────────────

    @on(DataTable.RowSelected, "#grid")
    def on_row_selected(self, event: DataTable.RowSelected) -> None:
        item = next((i for i in self._items if i["rk"] == str(event.row_key.value)), None)
        if item and item["type"] == "show":
            self.app.push_screen(DetailScreen(item))
        elif item and item["type"] == "movie":
            self.notify("Press [d] to delete, or select a TV show to view seasons.", timeout=2)

    def action_delete_item(self) -> None:
        t = self.query_one("#grid", DataTable)
        if not self._items or t.cursor_row < 0:
            return
        rows = list(t.ordered_rows)
        if t.cursor_row >= len(rows):
            return
        rk   = str(rows[t.cursor_row].key.value)
        item = next((i for i in self._items if i["rk"] == rk), None)
        if not item:
            return

        def on_result(result) -> None:
            if result is None:
                return
            if result == "configure":
                arr_app: ArrApp = "sonarr" if item["type"] == "show" else "radarr"
                self.app.push_screen(ArrConfigScreen(arr_app))
            else:
                self._do_delete(item, result["method"], result.get("delete_files", True))

        self.app.push_screen(DeleteModal(item), on_result)

    def action_go_back(self) -> None:
        self.app.pop_screen()

    def action_refresh_data(self) -> None:
        self._fetch()


# ── Detail Screen ──────────────────────────────────────────────────────────────

class DetailScreen(Screen):
    BINDINGS = [
        Binding("escape,q", "go_back", "Back"),
        Binding("d", "delete_item", "Delete"),
    ]
    CSS = """
    DetailScreen { layout: vertical; }
    #info {
        height: 4; dock: top;
        background: $surface; padding: 1 2;
        border-bottom: solid $surface-lighten-2;
    }
    #info-title { text-style: bold; }
    #info-meta  { color: $text-muted; }
    #seasons    { height: 1fr; }
    """

    # ── Messages ───────────────────────────────────────────────────────────────
    class DeleteStatus(Message):
        def __init__(self, text: str, ok: bool = True) -> None:
            self.text = text; self.ok = ok; super().__init__()

    def __init__(self, item: dict) -> None:
        super().__init__()
        self.item = item

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Container(id="info"):
            yield Static(
                f"{self.item['title']} ({self.item.get('year', '')})",
                id="info-title",
            )
            lw    = fmt_date(self.item.get("last_viewed_ts"))
            added = fmt_age(self.item.get("added_ts"))
            total = fmt_size(self.item.get("total_size", 0))
            safe  = fmt_size(self.item.get("safe_size", 0))
            yield Static(
                f"In library: {added}  ·  Last watched (any user): {lw}"
                f"  ·  Total: {total}  ·  Safe to remove: {safe}",
                id="info-meta",
            )
        yield DataTable(id="seasons", show_cursor=True)
        yield Footer()

    def on_mount(self) -> None:
        self.app.sub_title = self.item["title"]
        t = self.query_one("#seasons", DataTable)
        t.cursor_type = "row"
        t.add_columns("SEASON", "STATUS", "WATCHED", "LAST WATCHED", "SIZE", "ACTION")
        for s in self.item.get("seasons", []):
            label = "Specials" if s["number"] == 0 else f"Season {s['number']}"
            ep    = f"{s['watched_episodes']}/{s['total_episodes']} eps"
            t.add_row(label, s["status"], ep, s["last_viewed"], fmt_size(s["size"]), s["action"],
                      key=str(s["number"]))

    def on_unmount(self) -> None:
        self.app.sub_title = ""

    # ── Delete ─────────────────────────────────────────────────────────────────

    def action_delete_item(self) -> None:
        t = self.query_one("#seasons", DataTable)
        rows = list(t.ordered_rows)
        if not rows or t.cursor_row < 0 or t.cursor_row >= len(rows):
            return
        season_num = int(rows[t.cursor_row].key.value)
        season = next((s for s in self.item.get("seasons", []) if s["number"] == season_num), None)
        if not season:
            return

        def on_result(result) -> None:
            if result is None:
                return
            if result == "configure":
                self.app.push_screen(ArrConfigScreen("sonarr"))
            else:
                self._do_delete_season(season, result["method"])

        self.app.push_screen(DeleteModal(self.item, season=season), on_result)

    @work(thread=True)
    def _do_delete_season(self, season: dict, method: str) -> None:
        sn    = season["number"]
        label = "Specials" if sn == 0 else f"Season {sn}"
        try:
            if method == "arr":
                cfg = get_arr_cfg("sonarr")
                if not cfg:
                    raise RuntimeError("Sonarr is not configured.")
                tvdb = self.item.get("guids", {}).get("tvdb")
                if not tvdb:
                    raise RuntimeError("No TVDB ID found for this show.")
                rec = sonarr_find(cfg, tvdb)
                if not rec:
                    raise RuntimeError(f"Show not found in Sonarr (TVDB {tvdb}).")
                sonarr_delete_season(cfg, rec["id"], sn)
                _log.info("DELETED show %r %s via Sonarr (files removed, season unmonitored)",
                          self.item["title"], label)
                self.post_message(DetailScreen.DeleteStatus(
                    f"Deleted {label} via Sonarr (files removed, season unmonitored)."
                ))
            else:
                # Plex-only: delete each episode's metadata; files remain on disk
                plex_delete(self.item["rk"], self.app.token)  # type: ignore[attr-defined]
                _log.info("DELETED show %r %s from Plex library (files on disk unchanged)",
                          self.item["title"], label)
                self.post_message(DetailScreen.DeleteStatus(
                    f"Deleted {label} from Plex. Files on disk are unchanged."
                ))
        except Exception as e:
            _log.error("DELETE FAILED for show %r %s: %s", self.item.get("title", "?"), label, e)
            self.post_message(DetailScreen.DeleteStatus(f"Delete failed: {e}", ok=False))

    def on_detail_screen_delete_status(self, event: DeleteStatus) -> None:
        sev = "information" if event.ok else "error"
        self.notify(event.text, severity=sev)
        if event.ok:
            self.app.pop_screen()

    def action_go_back(self) -> None:
        self.app.pop_screen()


# ── App ────────────────────────────────────────────────────────────────────────

class PlexCleanupApp(App):
    TITLE = "Plex Cleanup"

    def __init__(self) -> None:
        super().__init__()
        self.token:    str                = ""
        self.activity: ActivitySet | None = None

    def on_mount(self) -> None:
        token = get_saved_token()
        if token:
            self.token = token
            self.push_screen(LibraryScreen())
        else:
            self.push_screen(AuthScreen())


if __name__ == "__main__":
    PlexCleanupApp().run()
