# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Mark Cheverton

"""Weekly ISFDB mirror refresh.

1. Log into the ISFDB wiki. It's behind Cloudflare's JS challenge, which is
   why this uses `cloudscraper` rather than plain `requests` — a bare
   `requests` session gets a 503 challenge page back instead of the site.
2. Scrape the "Database Backups" table on ISFDB_Downloads for the newest
   5.5-compatible MySQL dump's Google Drive link.
3. Download it via gdown (handles Google Drive's large-file confirm-token
   flow) to a local scratch file.
4. Import into a staging database, sanity-check row counts, then atomically
   swap it in for the live `isfdb` database via a single multi-table RENAME.
5. Delete the scratch file and the previous week's now-orphaned tables.

A failed run leaves last week's data live — the swap only happens after the
staging import is verified.
"""
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
import zipfile

import cloudscraper
import gdown
import pymysql
from bs4 import BeautifulSoup
from urllib.parse import urljoin

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("isfdb-refresh")

WIKI_USERNAME = os.environ["ISFDB_WIKI_USERNAME"]
WIKI_PASSWORD = os.environ["ISFDB_WIKI_PASSWORD"]
DB_HOST = os.environ.get("DB_HOST", "isfdb-db")
DB_PORT = int(os.environ.get("DB_PORT", "3306"))
DB_USER = os.environ.get("DB_USER", "root")
DB_PASSWORD = os.environ["MARIADB_ROOT_PASSWORD"]

DOWNLOADS_URL = "https://www.isfdb.org/wiki/index.php/ISFDB_Downloads"
LOGIN_URL = "https://www.isfdb.org/wiki/index.php/Special:UserLogin?returnto=ISFDB+Downloads"

# Sanity thresholds — ISFDB is a large, long-running database; a dump with
# fewer rows than this is almost certainly a truncated/failed download.
MIN_TITLES = 500_000
MIN_PUBS = 200_000

# Tables adapter.py actually queries. Everything else in ISFDB's dump is the
# site's own MediaWiki editing/moderation machinery — user accounts, edit
# history, view-count analytics, verification workflow queues — roughly half
# the dump's total row count (~12.9M of ~26M rows) that we never touch.
# Skipping it at import time (not just dropping it after) is what actually
# saves time, since writing the rows is the expensive part.
WANTED_TABLES = frozenset({
    "authors", "canonical_author", "languages", "notes", "pseudonyms",
    "pub_authors", "pub_content", "publishers", "pubs", "series", "titles",
})


def login_and_get_downloads_page() -> tuple[cloudscraper.CloudScraper, str]:
    scraper = cloudscraper.create_scraper()

    r1 = scraper.get(LOGIN_URL)
    r1.raise_for_status()
    soup = BeautifulSoup(r1.text, "html.parser")
    form = soup.find("form", {"name": "userlogin"})
    if form is None:
        raise RuntimeError("login form not found — ISFDB page structure may have changed")
    action = urljoin(r1.url, form["action"])
    payload = {inp.get("name"): inp.get("value", "") for inp in form.find_all("input") if inp.get("name")}
    payload["wpName"] = WIKI_USERNAME
    payload["wpPassword"] = WIKI_PASSWORD
    payload["wploginattempt"] = "Log in"

    r2 = scraper.post(action, data=payload)
    r2.raise_for_status()

    r3 = scraper.get(DOWNLOADS_URL)
    r3.raise_for_status()
    if "Login required" in r3.text[:600]:
        raise RuntimeError("login did not succeed — check ISFDB_WIKI_USERNAME/PASSWORD")

    return scraper, r3.text


def find_latest_backup_url(downloads_html: str) -> tuple[str, str]:
    """Returns (drive_url, date) for the newest 5.5-compatible MySQL dump."""
    soup = BeautifulSoup(downloads_html, "html.parser")
    best_date = None
    best_url = None
    for a in soup.find_all("a", href=True):
        if "drive.google.com" not in a["href"]:
            continue
        row = a.find_parent("tr")
        if row is None:
            continue
        table = a.find_parent("table")
        heading = table.find_previous(["h2", "h3", "h4"]) if table else None
        if not heading or "Database Backups" not in heading.get_text():
            continue
        header_row = table.find("tr")
        headers = [c.get_text(strip=True) for c in header_row.find_all(["td", "th"])]
        if "5.5-compatible" not in headers:
            continue
        cells = row.find_all(["td", "th"])
        if len(cells) < 2:
            continue
        date_text = cells[0].get_text(strip=True)
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_text):
            continue
        if best_date is None or date_text > best_date:
            best_date = date_text
            best_url = a["href"]

    if best_url is None:
        raise RuntimeError("no 5.5-compatible backup link found — ISFDB downloads page structure may have changed")
    return best_url, best_date


def drive_id_from_url(url: str) -> str:
    m = re.search(r"/d/([^/]+)/", url)
    if not m:
        raise RuntimeError(f"could not parse Google Drive file id from {url}")
    return m.group(1)


def extract_dump(zip_path: str, tmpdir: str) -> str:
    """ISFDB's backup download is a zip containing a single .sql file at an
    arbitrary (sometimes odd, e.g. Windows-path-shaped) internal name — pick
    the largest member rather than assuming a fixed filename."""
    with zipfile.ZipFile(zip_path) as zf:
        members = [i for i in zf.infolist() if not i.is_dir()]
        if not members:
            raise RuntimeError("backup zip is empty")
        largest = max(members, key=lambda i: i.file_size)
        log.info("extracting %s (%d bytes) from backup zip", largest.filename, largest.file_size)
        extracted_path = zf.extract(largest, path=tmpdir)
    return extracted_path


# Matches mysqldump's per-table section header comments, e.g.:
#   -- Table structure for table `authors`
#   -- Dumping data for table `authors`
_TABLE_MARKER_RE = re.compile(r"^-- (?:Table structure for table|Dumping data for table) `([^`]+)`")


def filter_unused_tables(dump_path: str, out_path: str, wanted: frozenset[str]) -> None:
    """Rewrite a mysqldump file, dropping CREATE/INSERT/LOCK/UNLOCK blocks for
    every table not in `wanted`.

    Safe by construction, not by careful semicolon-counting: mysqldump always
    emits a "-- Table structure for table `X`" / "-- Dumping data for table
    `X`" comment on its own line immediately before that table's DDL/data, so
    we only ever act on these marker lines, never try to parse statement
    boundaries inside the row data itself (which can contain arbitrary text,
    including semicolons in prose — e.g. `notes.note_note`). A table's data
    can span many physical lines (mysqldump splits large text-heavy tables
    into several INSERT statements), which is fine: we skip everything
    between one marker and the next regardless of line count.

    Earlier version also treated *any* other `--`-prefixed line as a reset
    to "keep" — meant to protect global preamble/footer content outside any
    table's section, but wrong: mysqldump wraps every marker in bare `--`
    divider lines (a blank "--" comment line immediately before and after
    each marker), so that fallback fired on the very next line after every
    marker and undid the skip decision before a single data line was
    reached. Confirmed against a real dump: filtering barely changed the
    file size (8KB of 1.6GB) when it should have dropped ~12.9M of ~26M
    rows. Fixed by only ever toggling `skip` on an actual table marker —
    the preamble is naturally kept because `skip` starts False, and the
    file's small SET-statement footer (harmless to lose on a fresh import
    into an empty database) is the only content that can be affected if
    the dump's last table happens to be unwanted.
    """
    kept_tables: set[str] = set()
    skip = False
    lines_in = lines_out = 0

    with open(dump_path, "r", encoding="utf-8", errors="replace") as fin, \
            open(out_path, "w", encoding="utf-8") as fout:
        for line in fin:
            lines_in += 1
            m = _TABLE_MARKER_RE.match(line)
            if m:
                table = m.group(1)
                skip = table not in wanted
                if not skip:
                    kept_tables.add(table)

            if not skip:
                fout.write(line)
                lines_out += 1

    missing = wanted - kept_tables
    if missing:
        raise RuntimeError(
            f"filter_unused_tables: expected tables not found in dump (dump format may "
            f"have changed): {sorted(missing)}"
        )
    log.info(
        "filtered dump: %d/%d lines kept, %d/%d wanted tables found",
        lines_out, lines_in, len(kept_tables), len(wanted),
    )


def db_conn(database: str | None = None):
    return pymysql.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASSWORD,
        database=database, autocommit=True, connect_timeout=10,
        cursorclass=pymysql.cursors.DictCursor,
    )


def import_dump(dump_path: str, target_db: str):
    conn = db_conn()
    with conn.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS `{target_db}`")
        cur.execute(f"CREATE DATABASE `{target_db}` DEFAULT CHARACTER SET utf8mb4")
    conn.close()

    log.info("importing dump into %s (this takes a couple of minutes)...", target_db)
    with open(dump_path, "rb") as f:
        result = subprocess.run(
            ["mariadb", "-h", DB_HOST, "-P", str(DB_PORT), "-u", DB_USER,
             f"-p{DB_PASSWORD}", "--max_allowed_packet=256M", target_db],
            stdin=f, capture_output=True, text=True,
        )
    if result.returncode != 0:
        raise RuntimeError(f"mariadb import failed: {result.stderr[-2000:]}")


def build_search_indexes(database: str):
    """FULLTEXT indexes for the /search endpoint. Not part of ISFDB's own
    dump — a plain LIKE '%...%' scan over ~2.5M rows in `titles` took 200+
    seconds (discovered in production); MATCH...AGAINST needs these to stay
    under a second. Rebuilt every refresh since each one is a fresh staging
    database with none of the previous week's indexes."""
    log.info("building search indexes on %s...", database)
    conn = db_conn(database)
    with conn.cursor() as cur:
        cur.execute(f"ALTER TABLE `{database}`.titles ADD FULLTEXT INDEX ft_title_title (title_title)")
        cur.execute(f"ALTER TABLE `{database}`.authors ADD FULLTEXT INDEX ft_author_canonical (author_canonical)")
    conn.close()


def sanity_check(database: str):
    conn = db_conn(database)
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) AS n FROM `{database}`.titles")
        titles = cur.fetchone()["n"]
        cur.execute(f"SELECT count(*) AS n FROM `{database}`.pubs")
        pubs = cur.fetchone()["n"]
    conn.close()
    log.info("staging row counts — titles=%d pubs=%d", titles, pubs)
    if titles < MIN_TITLES or pubs < MIN_PUBS:
        raise RuntimeError(
            f"staging import looks truncated (titles={titles}, pubs={pubs}); "
            f"expected at least titles={MIN_TITLES}, pubs={MIN_PUBS} — aborting swap"
        )


def atomic_swap(staging_db: str, live_db: str):
    old_db = f"{live_db}_old"
    conn = db_conn()
    with conn.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS `{old_db}`")
        cur.execute(f"CREATE DATABASE `{old_db}` DEFAULT CHARACTER SET utf8mb4")

        cur.execute(f"SHOW TABLES FROM `{live_db}`")
        live_tables = [r[f"Tables_in_{live_db}"] for r in cur.fetchall()]
        cur.execute(f"SHOW TABLES FROM `{staging_db}`")
        staging_tables = [r[f"Tables_in_{staging_db}"] for r in cur.fetchall()]

        renames = []
        for t in live_tables:
            renames.append(f"`{live_db}`.`{t}` TO `{old_db}`.`{t}`")
        for t in staging_tables:
            renames.append(f"`{staging_db}`.`{t}` TO `{live_db}`.`{t}`")

        if renames:
            cur.execute("RENAME TABLE " + ", ".join(renames))
            log.info("atomic swap complete — %d live tables archived, %d staging tables promoted",
                      len(live_tables), len(staging_tables))

        cur.execute(f"DROP DATABASE IF EXISTS `{old_db}`")
        cur.execute(f"DROP DATABASE IF EXISTS `{staging_db}`")
    conn.close()


def warm_caches(database: str):
    """Preload MyISAM indexes (including the FULLTEXT ones) into the key
    buffer right after a swap, so the first real searches don't pay for a
    cold key buffer against a table that's had zero queries against it.
    `LOAD INDEX INTO CACHE` reads every index block for the named tables —
    unlike firing sample search queries, it doesn't depend on guessing which
    terms real users will search next; it guarantees full coverage. Requires
    key_buffer_size to actually be large enough to hold it all (see
    mariadb-statefulset.yaml in the k8s repo) or this just thrashes instead
    of helping."""
    log.info("warming key buffer for %s...", database)
    start = time.time()
    conn = db_conn(database)
    with conn.cursor() as cur:
        cur.execute(
            f"LOAD INDEX INTO CACHE `{database}`.titles, `{database}`.authors, "
            f"`{database}`.canonical_author, `{database}`.pubs, `{database}`.pub_content"
        )
    conn.close()
    log.info("key buffer warm in %.0fs", time.time() - start)


def main():
    log.info("checking ISFDB downloads page for a new backup...")
    scraper, html = login_and_get_downloads_page()
    drive_url, backup_date = find_latest_backup_url(html)
    log.info("latest 5.5-compatible backup: %s (%s)", backup_date, drive_url)

    with tempfile.TemporaryDirectory() as tmpdir:
        zip_path = os.path.join(tmpdir, "isfdb-backup.zip")
        file_id = drive_id_from_url(drive_url)
        log.info("downloading via gdown (id=%s)...", file_id)
        start = time.time()
        gdown.download(f"https://drive.google.com/uc?id={file_id}", output=zip_path, quiet=False)
        log.info("download complete in %.0fs, size=%d bytes", time.time() - start, os.path.getsize(zip_path))

        dump_path = extract_dump(zip_path, tmpdir)

        filtered_path = os.path.join(tmpdir, "isfdb-filtered.sql")
        filter_unused_tables(dump_path, filtered_path, WANTED_TABLES)
        os.remove(dump_path)  # the ~1.6GB unfiltered copy — no need to hold both

        import_dump(filtered_path, "isfdb_staging")
        build_search_indexes("isfdb_staging")
        sanity_check("isfdb_staging")
        atomic_swap("isfdb_staging", "isfdb")
        warm_caches("isfdb")

    log.info("refresh complete — mirror now at %s", backup_date)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.exception("refresh failed")
        sys.exit(1)
