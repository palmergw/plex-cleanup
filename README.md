# Plex Cleanup

An interactive terminal UI for identifying and removing unwatched or stale content from your Plex media server. Integrates with Radarr and Sonarr to handle deletion properly so content isn't automatically re-downloaded.

---

## Features

- Connects to your Plex server using your Plex.tv credentials (no manual token required)
- Loads watch history across **all server users**, not just the authenticated account
- Scans movie and TV libraries for content that hasn't been watched within a configurable window
- Displays how long each item has been in the library to avoid flagging new additions
- Shows ratings (critic or audience) sourced from whatever provider Plex has matched (RT, IMDb, TMDB)
- TV shows break down watch status per season with conservative cleanup recommendations:
  - **REMOVE** — fully watched or never watched with no in-progress series nearby
  - **KEEP** — currently in progress
  - **PENDING** — never watched, but another season is in progress (don't remove mid-series)
- Integrates with **Radarr** and **Sonarr** for proper deletion (removes files, untracks the item)
- Warns you if Radarr/Sonarr isn't configured before deleting, with a "don't show again" option
- Cursor position is preserved after deletes and filter changes

---

## Requirements

- Python 3.11+
- A running Plex Media Server reachable over the network
- `pip install -r requirements.txt`

```
requests>=2.31.0
textual>=0.60.0
```

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure the Plex server URL

Open `main.py` and update the `PLEX_SERVER` constant at the top of the file to point to your Plex instance:

```python
PLEX_SERVER = "https://plex.yourserver.com"
```

### 3. Run the app

```bash
python main.py
```

On first launch you will be prompted to sign in with your **Plex.tv** account (email/username and password). Two-factor authentication is supported. Your auth token is saved to `~/.plex-cleanup.json` so subsequent launches skip the sign-in screen.

---

## Radarr / Sonarr Integration (Optional)

Without this integration, the app can still delete items directly from Plex, but the files will remain on disk and Radarr/Sonarr will re-download them on the next search cycle.

### Configure from within the app

Press **`s`** on the library selection screen to open Settings. Enter the base URL and API key for each service and use **Test Connection** to verify before saving.

| Service | Default port | API key location |
|---------|-------------|-----------------|
| Radarr  | 7878        | Settings → General → Security |
| Sonarr  | 8989        | Settings → General → Security |

Configuration is saved to `~/.plex-cleanup.json` alongside your Plex token.

### What the integration does

- **Movies (Radarr):** Finds the movie by TMDB ID, deletes files from disk, and removes it from Radarr's tracking. Content is **not** added to the import exclusion list — if you want to prevent re-download, unmonitor the item in Radarr first or remove it from your indexer lists.
- **Full TV series (Sonarr):** Finds the series by TVDB ID, deletes all files, and removes it from Sonarr's tracking.
- **Individual seasons (Sonarr):** Deletes all episode files for that season and sets the season to unmonitored so Sonarr won't re-queue it.

---

## Using the App

### Library Selection screen

Displays all libraries on your Plex server with item counts. Watch history for all users loads in the background — a status line at the bottom confirms when it's ready.

| Key | Action |
|-----|--------|
| `Enter` | Open selected library |
| `s` | Open integration settings (Radarr / Sonarr) |
| `q` | Quit |

### Grid screen (library contents)

Shows items matching the current filter. The aggregate size of the filtered list is displayed in the top-right of the filter bar.

**Filter bar options:**

| Control | Description |
|---------|-------------|
| **Days** | Items not watched within this many days are shown (up to 4 digits) |
| **Never only** | Show only items that have never been watched by anyone |
| **Safe only** | Show only items where all content is safe to remove |
| **Rating** | Switch between Critic and Audience rating source |
| **Sort** | Sort by size, last watched, date added, rating, or title |
| **Apply** | Apply day/never filter changes (sort and rating change instantly) |

**Column descriptions:**

| Column | Description |
|--------|-------------|
| TITLE | Title and year |
| LAST WATCHED | Most recent watch date across all users |
| IN LIBRARY | How long the item has been in the library |
| SIZE / TOTAL | File size on disk |
| SAFE | Size of content safe to remove (TV only) |
| STATUS | All Watched / In Progress / Unwatched / Mixed (TV only) |
| RATING | Critic or audience score with source (RT, IMDb, TMDB) |

| Key | Action |
|-----|--------|
| `Enter` | Open season detail (TV shows only) |
| `d` | Delete selected item |
| `r` | Refresh list from Plex |
| `Escape` | Back to library selection |

### Season Detail screen (TV shows)

Breaks the selected show down by season with watch status, episode counts, last watched date, file size, and cleanup recommendation per season.

| Column | Description |
|--------|-------------|
| STATUS | Fully Watched / In Progress / Never Watched |
| WATCHED | Episodes watched out of total |
| ACTION | REMOVE / KEEP / PENDING |

| Key | Action |
|-----|--------|
| `d` | Delete selected season |
| `Escape` / `q` | Back to grid |

### Deleting content

Pressing `d` opens a confirmation modal. If Radarr/Sonarr is configured for the relevant library type, you will see two options:

- **Delete via Radarr/Sonarr** — removes files from disk and untracks the item. Recommended.
- **Delete from Plex only** — removes the Plex library entry. Files remain on disk and Radarr/Sonarr will be unaware of the deletion.

If Radarr/Sonarr is not configured, the modal explains the risk and offers to take you to the settings screen or proceed with a Plex-only delete. A "don't show again" checkbox suppresses this warning permanently for that service.

After a successful delete, the item is removed from the list immediately and the cursor moves to the next item.

---

## Watch History and Staleness Logic

The app pulls complete watch history from `/status/sessions/history/all` across all server users. A show or movie is considered stale if the most recent watch event (any user, any episode) falls before the configured cutoff.

For TV shows, if `grandparentRatingKey` is missing from history entries (can happen after a library rematch), the app falls back to deriving the show-level last-watched timestamp from the max of its individual episode timestamps, so the show-level date is always accurate.

**Season safety rules:**

| Season status | Any in-progress season exists? | Recommendation |
|--------------|-------------------------------|----------------|
| Fully Watched | Either | REMOVE |
| In Progress | — | KEEP |
| Never Watched | Yes | PENDING |
| Never Watched | No | REMOVE |

The intent is to never recommend removing content from a series someone is actively working through.

---

## Configuration file

Stored at `~/.plex-cleanup.json`. Fields:

```json
{
  "token": "your-plex-auth-token",
  "radarr": {
    "url": "http://localhost:7878",
    "api_key": "your-radarr-api-key"
  },
  "sonarr": {
    "url": "http://localhost:8989",
    "api_key": "your-sonarr-api-key"
  },
  "skip_arr_prompt_radarr": false,
  "skip_arr_prompt_sonarr": false
}
```

Delete this file to sign out and reset all configuration.
