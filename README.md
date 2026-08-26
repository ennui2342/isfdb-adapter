# isfdb-adapter

A small JSON API that fronts a self-hosted mirror of the [Internet Speculative
Fiction Database](https://www.isfdb.org/) (ISFDB) — deep bibliographic data
for SFF, especially older, small-press, and non-US editions that Open
Library, Google Books, and Hardcover often don't carry at all.

This exists because **ISFDB has no public API.** The site is behind
Cloudflare, and the only distribution of its data is a weekly MySQL dump.
ISFDB publishes each week's dump to a shared, publicly-viewable Google
Drive folder; `refresh.py` handles the whole pipeline — find the current
week's file in that folder, download, import — so you always have a
local, queryable mirror. `adapter.py` is the API layer in front of it.

It was built as the backend for [Librarium](https://github.com/FireBall1725/librarium-api)'s
ISFDB metadata provider (see that PR for the client side), but the API is
generic enough to use from anything else that wants ISFDB data.

## Architecture

```
ISFDB Backups Drive folder (public) ──weekly──▶ refresh.py ──imports──▶ MariaDB ◀──queries── adapter.py ──JSON──▶ your app
```

- **`refresh.py`** — lists ISFDB's shared "Backups" Google Drive folder
  (public, no login needed) for the current week's dump, downloads it
  (`gdown`), extracts and filters it down to the tables this adapter
  actually needs (~50% smaller import), and atomically swaps it into the
  live database via `RENAME TABLE` — a failed or interrupted refresh leaves
  last week's data live, never a half-imported DB. Run it on a schedule
  (cron, a Kubernetes CronJob, whatever you've got); see
  `docker-compose.yml` for a manual/cron invocation.
  This used to go via the ISFDB wiki's login-gated downloads page instead
  (`cloudscraper` handling Cloudflare's JS challenge, then a form POST for
  the wiki login) — that broke when Cloudflare's protection started
  blocking `cloudscraper` too. ISFDB shared the Drive folder directly as
  the replacement.
- **`adapter.py`** — a FastAPI app that queries the mirror and returns JSON
  shaped for easy consumption (ISBN-10/13 normalization, MySQL's zero-date
  quirks cleaned up, FULLTEXT-indexed search instead of `LIKE '%...%'` full
  scans). Stateless, safe to run multiple replicas.

## Quickstart

```sh
cp .env.example .env   # fill in MARIADB_ROOT_PASSWORD
docker compose up -d db adapter
docker compose --profile refresh run --rm refresh   # first import — takes ~20-25 minutes
curl http://localhost:8080/isbn/0441172717
```

The database is empty until you run `refresh` at least once. After that,
put `refresh` on a schedule (weekly is enough — ISFDB's own backup is
weekly) via host cron, a systemd timer, or a Kubernetes CronJob if you're
running this in a cluster instead.

### Environment variables

| Variable | Used by | Required | Notes |
|---|---|---|---|
| `MARIADB_ROOT_PASSWORD` | both | yes | |
| `DB_HOST` / `DB_PORT` / `DB_USER` / `DB_NAME` | both | no | default to `db` / `3306` / `root` / `isfdb` |

## API

All endpoints return `404` for "not found" and `503` on a database error
(e.g. mid-refresh). Book-length works only (`NOVEL`, `COLLECTION`,
`OMNIBUS`, `CHAPBOOK`) — short fiction, essays, and interior art aren't
included.

### `GET /health`

Liveness/readiness check. `{"status": "ok"}`.

### `GET /isbn/{isbn}`

Look up a single edition by ISBN-10 or ISBN-13.

```
curl http://localhost:8080/isbn/0441172717
```
```json
{
  "provider": "isfdb", "provider_display": "ISFDB",
  "title": "Dune", "subtitle": "", "authors": ["Frank Herbert"],
  "publisher": "Ace Books", "publish_date": "2010",
  "isbn_10": "0441172717", "isbn_13": "9780441172719",
  "description": "", "cover_url": "", "language": "eng", "page_count": 883,
  "categories": []
}
```

Note the caveat this adapter can't fully solve: ISFDB sometimes reuses one
ISBN across several distinct print runs of the same book (a UK paperback
publisher reprinting years apart under the original ISBN is common). This
endpoint returns *some* matching edition, not necessarily the one you meant
— if you need a specific printing, use `/search` instead, which returns
every edition separately, and pick the right one from title/publisher/year.

### `GET /search?q=&limit=20&editions_per_title=10`

Freetext title/author search. Title and author are matched independently
(natural-language relevance, not a strict AND — a query like "camp
concentration disch" matches on title words and the author's surname
separately) and scored per matched *title* (work), then every edition of
each matched title is returned — up to `editions_per_title` each, ordered
by known publish year, capped overall at `limit` results. Returns the same
shape as `/isbn/{isbn}`, one object per edition.

```
curl "http://localhost:8080/search?q=dune"
```

### `GET /series/search?q=&limit=20`

```json
[{
  "provider": "isfdb", "provider_display": "ISFDB",
  "name": "Dune", "description": "", "total_count": 6, "is_complete": false,
  "cover_url": "", "external_id": "63762", "external_source": "isfdb",
  "status": "", "original_language": "", "publication_year": null,
  "demographic": "", "genres": [],
  "url": "https://www.isfdb.org/cgi-bin/pl.cgi?63762"
}]
```

### `GET /series/{series_id}/volumes`

Ordered volume list for a series (by series position, falling back to
copyright date for unpositioned entries). `404` if the series doesn't
exist or has no book-length entries.

```json
[{"position": 1.0, "title": "Dune", "release_date": "1965", "cover_url": "https://...", "external_id": "2036"}]
```

### `GET /authors/search?q=&limit=20`

```json
[{"external_id": "1234", "name": "Frank Herbert", "bio": "", "photo_url": ""}]
```

### `GET /authors/{author_id}`

Full author profile: bio (from ISFDB's own author note, if any),
pseudonym cross-references, and a bibliography with one representative
edition's ISBN/cover per work.

```json
{
  "provider": "isfdb", "external_id": "1234", "name": "Frank Herbert",
  "legal_name": "", "bio": "...",
  "born_date": "1920-10-08", "died_date": "1986-02-11",
  "nationality": "", "photo_url": "",
  "pseudonym_ids": [],
  "works": [{"title": "Dune", "isbn_13": "...", "isbn_10": "...", "publish_year": 1965, "cover_url": "..."}]
}
```

## A note on dates

ISFDB stores partial/unknown dates as e.g. `"1973-00-00"` (year known,
month/day not) or `"0000-00-00"` (nothing known) — not valid ISO 8601 and
not something most consumers expect. This adapter normalizes them before
returning: `"1973-00-00"` → `"1973"`, `"1974-09-00"` → `"1974-09"`, full
dates pass through unchanged, and the placeholder years ISFDB uses for
"unknown" (`0000`, `8888`) come back as an empty string rather than a
literal year. If you're consuming this API, expect `publish_date` /
`born_date` / `died_date` to be a bare year, `YYYY-MM`, a full `YYYY-MM-DD`,
or `""` — never ISFDB's raw zero-padded form.

## ISFDB's data

The weekly backup is provided by the ISFDB project for non-commercial use;
check [isfdb.org](https://www.isfdb.org/wiki/index.php/ISFDB:Policy) for
their current terms before relying on this for anything beyond personal
use, and consider contributing back to ISFDB itself if you find gaps in
its data — it's a volunteer-maintained wiki.

## License

AGPL-3.0 — see [LICENSE](LICENSE). If you run a modified version of this
adapter as a network service, the AGPL requires you to make your modified
source available to users of that service.
