# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Mark Cheverton

"""ISFDB mirror adapter — thin JSON API in front of a local ISFDB MySQL/MariaDB
mirror. Originally built as the backend for Librarium's ISFDB metadata
provider, but generic enough for any consumer that wants ISFDB data.

Endpoints:
  GET /isbn/{isbn}                book lookup by ISBN-10 or ISBN-13
  GET /search?q=                  freetext title/author search
  GET /series/search?q=           freetext series-name search
  GET /series/{series_id}/volumes ordered per-volume list for a series
  GET /authors/search?q=          freetext author-name search
  GET /authors/{author_id}        full author profile + bibliography
  GET /health                     liveness/readiness

"Book-like" title types (excludes short fiction, essays, art, reviews, etc.):
"""
import os
import re
from contextlib import contextmanager

import pymysql
import pymysql.cursors
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

app = FastAPI(title="isfdb-adapter")

DB_HOST = os.environ.get("DB_HOST", "isfdb-db")
DB_PORT = int(os.environ.get("DB_PORT", "3306"))
DB_USER = os.environ.get("DB_USER", "root")
DB_PASSWORD = os.environ.get("MARIADB_ROOT_PASSWORD", "")
DB_NAME = os.environ.get("DB_NAME", "isfdb")

BOOK_TTYPES = ("NOVEL", "COLLECTION", "OMNIBUS", "CHAPBOOK")

_ISBN_CLEAN_RE = re.compile(r"[^0-9Xx]")


@contextmanager
def db():
    conn = pymysql.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME,
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
    )
    try:
        yield conn
    finally:
        conn.close()


def clean_isbn(raw: str) -> str:
    return _ISBN_CLEAN_RE.sub("", raw).upper()


def isbn10_to_13(isbn10: str) -> str | None:
    if len(isbn10) != 10:
        return None
    core = "978" + isbn10[:9]
    total = sum((1 if i % 2 == 0 else 3) * int(d) for i, d in enumerate(core))
    check = (10 - (total % 10)) % 10
    return core + str(check)


def isbn13_to_10(isbn13: str) -> str | None:
    if len(isbn13) != 13 or not isbn13.startswith("978"):
        return None
    core = isbn13[3:12]
    total = sum((10 - i) * int(d) for i, d in enumerate(core))
    check = (11 - (total % 11)) % 11
    check_char = "X" if check == 10 else str(check)
    return core + check_char


def isbn_candidates(raw: str) -> list[str]:
    cleaned = clean_isbn(raw)
    candidates = [cleaned]
    if len(cleaned) == 10:
        c13 = isbn10_to_13(cleaned)
        if c13:
            candidates.append(c13)
    elif len(cleaned) == 13:
        c10 = isbn13_to_10(cleaned)
        if c10:
            candidates.append(c10)
    return candidates


def date_str(value) -> str:
    """ISFDB stores partial/zero dates ("1973-00-00", "0000-00-00") that
    MySQL can't represent as a real date; PyMySQL falls back to returning
    the raw string for those instead of raising.

    Normalize month/day "00" (ISFDB's "unknown" placeholder) down to
    whatever precision is actually known — "1973-00-00" -> "1973",
    "1974-09-00" -> "1974-09" — rather than passing it through raw.
    Librarium's date parser doesn't recognize ISFDB's zero-padding
    convention and silently drops the whole value to null when it fails
    to parse, discarding even the year we do know (found via a real
    apply: a book's publish date came back empty after refreshing from
    an ISFDB edition whose day/month were unknown).
    """
    if not value:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    s = str(value)
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if not m:
        return s
    year, month, day = m.groups()
    if year in ("0000", "8888"):
        return ""
    if month == "00":
        return year
    if day == "00":
        return f"{year}-{month}"
    return s


def date_year(value) -> int | None:
    s = date_str(value)
    if len(s) >= 4 and s[:4].isdigit() and s[:4] != "0000":
        return int(s[:4])
    return None


def parse_page_count(pub_pages: str | None) -> int | None:
    """ISFDB page counts are freetext (e.g. "158", "xii+240", "158+16pp").
    Extract the first plausible integer."""
    if not pub_pages:
        return None
    m = re.search(r"\d+", pub_pages)
    return int(m.group(0)) if m else None


def book_result_from_pub(cur, pub: dict) -> dict:
    """Build a BookResult-shaped dict from a pubs row (+ its title/authors)."""
    cur.execute(
        """
        SELECT t.title_id, t.title_title, t.title_language, t.series_id, t.title_seriesnum
        FROM pub_content pc
        JOIN titles t ON t.title_id = pc.title_id
        WHERE pc.pub_id = %s AND t.title_ttype IN %s
        LIMIT 1
        """,
        (pub["pub_id"], BOOK_TTYPES),
    )
    title_row = cur.fetchone()

    authors: list[str] = []
    if title_row:
        cur.execute(
            """
            SELECT a.author_canonical
            FROM canonical_author ca
            JOIN authors a ON a.author_id = ca.author_id
            WHERE ca.title_id = %s
            """,
            (title_row["title_id"],),
        )
        authors = [r["author_canonical"] for r in cur.fetchall()]
    if not authors:
        cur.execute(
            """
            SELECT a.author_canonical
            FROM pub_authors pa
            JOIN authors a ON a.author_id = pa.author_id
            WHERE pa.pub_id = %s
            """,
            (pub["pub_id"],),
        )
        authors = [r["author_canonical"] for r in cur.fetchall()]

    language = None
    if title_row and title_row.get("title_language"):
        cur.execute("SELECT lang_code FROM languages WHERE lang_id = %s", (title_row["title_language"],))
        lrow = cur.fetchone()
        language = lrow["lang_code"] if lrow else None

    publisher = None
    if pub.get("publisher_id"):
        cur.execute("SELECT publisher_name FROM publishers WHERE publisher_id = %s", (pub["publisher_id"],))
        prow = cur.fetchone()
        publisher = prow["publisher_name"] if prow else None

    isbn = clean_isbn(pub.get("pub_isbn") or "")
    isbn10 = isbn13 = None
    if len(isbn) == 10:
        isbn10 = isbn
        isbn13 = isbn10_to_13(isbn)
    elif len(isbn) == 13:
        isbn13 = isbn
        isbn10 = isbn13_to_10(isbn)

    return {
        "provider": "isfdb",
        "provider_display": "ISFDB",
        "title": (title_row or {}).get("title_title") or pub.get("pub_title") or "",
        "subtitle": "",
        "authors": authors,
        "publisher": publisher or "",
        "publish_date": date_str(pub.get("pub_year")),
        "isbn_10": isbn10 or "",
        "isbn_13": isbn13 or "",
        "description": "",
        "cover_url": pub.get("pub_frontimage") or "",
        "language": language or "",
        "page_count": parse_page_count(pub.get("pub_pages")),
        "categories": [],
        # Raw ISFDB pub_ptype ("hc", "tp", "pb", "ebook", "audio CD", ...) —
        # consumers map this to their own format/binding vocabulary rather
        # than the adapter guessing at a canonical mapping that's really
        # Librarium-specific (see librarium-api's mapISFDBBinding).
        "binding": pub.get("pub_ptype") or "",
        # extras, not part of Librarium's BookResult but useful for debugging
        "_isfdb_pub_id": pub["pub_id"],
        "_isfdb_title_id": (title_row or {}).get("title_id"),
        "_isfdb_series_id": (title_row or {}).get("series_id"),
        "_isfdb_series_num": (title_row or {}).get("title_seriesnum"),
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/isbn/{isbn}")
def lookup_isbn(isbn: str):
    # Query pub_isbn directly (unwrapped) so MySQL can use its existing
    # index. An earlier version wrapped it in REPLACE(REPLACE(...)) to
    # tolerate hyphens/spaces "just in case" — that defensive wrapping
    # disables the index and forces a full ~950K-row scan on every lookup
    # (took 2.5+ minutes cold in production). ISFDB's pub_isbn is verified
    # clean (checked: 0 of 664044 non-empty values contain a hyphen or
    # space), so candidates are normalized client-side instead.
    candidates = isbn_candidates(isbn)
    with db() as conn, conn.cursor() as cur:
        placeholders = ", ".join(["%s"] * len(candidates))
        cur.execute(
            f"SELECT * FROM pubs WHERE pub_isbn IN ({placeholders}) "
            f"ORDER BY pub_year DESC LIMIT 1",
            candidates,
        )
        pub = cur.fetchone()
        if not pub:
            raise HTTPException(status_code=404, detail="isbn not found")
        return book_result_from_pub(cur, pub)


@app.get("/search")
def search_books(q: str, limit: int = 20, editions_per_title: int = 10):
    """Title/author freetext search.

    Uses MATCH...AGAINST against FULLTEXT indexes (ft_title_title,
    ft_author_canonical — created by refresh.py after each import) rather
    than LIKE '%...%'. A leading-wildcard LIKE can't use any index and does
    a full scan; at ~2.5M rows in `titles` that timed out client-side
    (observed during development — a 20s search took 200+ seconds server
    side before being killed). MATCH...AGAINST returns in well under a
    second on the same data.

    Title and author are matched as two independent NATURAL LANGUAGE MODE
    branches (a query mixes title words and an author surname, e.g. "camp
    concentration disch", and neither field alone contains all of it — a
    strict AND within one column would match nothing). Scores are UNION
    ALL'd and summed per title_id rather than UNION'd and dedup-on-first-seen,
    so a title matching *both* branches (the actual right book) outranks
    titles that only coincidentally match one branch (e.g. other books by
    the same author, or foreign-language editions that share the English
    title string) — without that, we saw an exact double-match (title score
    18.0 + author score 10.8) rank below same-author noise that only ever
    hit the author branch once.

    Each matched title can have many editions in ISFDB (Camp Concentration
    has ~15) — return up to `editions_per_title` of them instead of
    collapsing to a single "earliest" one, so the specific edition a user
    owns has a chance of showing up. Editions with an unknown/placeholder
    ISFDB date ("0000-00-00", "8888-00-00") sort last rather than first.
    """
    if len(q.strip()) < 2:
        raise HTTPException(status_code=400, detail="query too short")
    term = q.strip()
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT title_id, SUM(score) AS total_score
            FROM (
                (SELECT t.title_id, MATCH(t.title_title) AGAINST(%s IN NATURAL LANGUAGE MODE) AS score
                 FROM titles t
                 WHERE t.title_ttype IN %s AND MATCH(t.title_title) AGAINST(%s IN NATURAL LANGUAGE MODE))
                UNION ALL
                (SELECT t.title_id, MATCH(a.author_canonical) AGAINST(%s IN NATURAL LANGUAGE MODE) AS score
                 FROM titles t
                 JOIN canonical_author ca ON ca.title_id = t.title_id
                 JOIN authors a ON a.author_id = ca.author_id
                 WHERE t.title_ttype IN %s AND MATCH(a.author_canonical) AGAINST(%s IN NATURAL LANGUAGE MODE))
            ) matches
            GROUP BY title_id
            ORDER BY total_score DESC
            LIMIT %s
            """,
            (term, BOOK_TTYPES, term, term, BOOK_TTYPES, term, limit),
        )
        title_ids = [r["title_id"] for r in cur.fetchall()]
        results = []
        for tid in title_ids:
            if len(results) >= limit:
                break
            cur.execute(
                """
                SELECT p.* FROM pub_content pc
                JOIN pubs p ON p.pub_id = pc.pub_id
                WHERE pc.title_id = %s
                ORDER BY (p.pub_year IS NULL OR p.pub_year IN ('0000-00-00', '8888-00-00')) ASC,
                         p.pub_year ASC
                LIMIT %s
                """,
                (tid, min(editions_per_title, limit - len(results))),
            )
            for pub in cur.fetchall():
                results.append(book_result_from_pub(cur, pub))
        return results


@app.get("/series/search")
def search_series(q: str, limit: int = 20):
    if len(q.strip()) < 2:
        raise HTTPException(status_code=400, detail="query too short")
    like = f"%{q.strip()}%"
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT series_id, series_title FROM series WHERE series_title LIKE %s LIMIT %s",
            (like, limit),
        )
        rows = cur.fetchall()
        out = []
        for row in rows:
            cur.execute(
                "SELECT count(*) AS n FROM titles WHERE series_id = %s AND title_ttype IN %s",
                (row["series_id"], BOOK_TTYPES),
            )
            count_row = cur.fetchone()
            out.append({
                "provider": "isfdb",
                "provider_display": "ISFDB",
                "name": row["series_title"],
                "description": "",
                "total_count": count_row["n"] if count_row else None,
                "is_complete": False,
                "cover_url": "",
                "external_id": str(row["series_id"]),
                "external_source": "isfdb",
                "status": "",
                "original_language": "",
                "publication_year": None,
                "demographic": "",
                "genres": [],
                "url": f"https://www.isfdb.org/cgi-bin/pl.cgi?{row['series_id']}",
            })
        return out


@app.get("/series/{series_id}/volumes")
def series_volumes(series_id: int):
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT title_id, title_title, title_seriesnum, title_copyright
            FROM titles
            WHERE series_id = %s AND title_ttype IN %s
            ORDER BY title_seriesnum IS NULL, title_seriesnum ASC, title_copyright ASC
            """,
            (series_id, BOOK_TTYPES),
        )
        titles = cur.fetchall()
        if not titles:
            raise HTTPException(status_code=404, detail="series not found or has no book-length volumes")

        out = []
        for t in titles:
            cur.execute(
                """
                SELECT p.pub_frontimage FROM pub_content pc
                JOIN pubs p ON p.pub_id = pc.pub_id
                WHERE pc.title_id = %s AND p.pub_frontimage IS NOT NULL AND p.pub_frontimage <> ''
                ORDER BY p.pub_year ASC
                LIMIT 1
                """,
                (t["title_id"],),
            )
            cover = cur.fetchone()
            out.append({
                "position": float(t["title_seriesnum"]) if t["title_seriesnum"] is not None else 0.0,
                "title": t["title_title"],
                "release_date": date_str(t.get("title_copyright")),
                "cover_url": (cover or {}).get("pub_frontimage") or "",
                "external_id": str(t["title_id"]),
            })
        return out


@app.get("/authors/search")
def search_authors(q: str, limit: int = 20):
    if len(q.strip()) < 2:
        raise HTTPException(status_code=400, detail="query too short")
    like = f"%{q.strip()}%"
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT author_id, author_canonical FROM authors WHERE author_canonical LIKE %s LIMIT %s",
            (like, limit),
        )
        return [
            {
                "external_id": str(r["author_id"]),
                "name": r["author_canonical"],
                "bio": "",
                "photo_url": "",
            }
            for r in cur.fetchall()
        ]


@app.get("/authors/{author_id}")
def fetch_author(author_id: int):
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM authors WHERE author_id = %s", (author_id,))
        author = cur.fetchone()
        if not author:
            raise HTTPException(status_code=404, detail="author not found")

        bio = ""
        if author.get("note_id"):
            cur.execute("SELECT note_note FROM notes WHERE note_id = %s", (author["note_id"],))
            note = cur.fetchone()
            bio = (note or {}).get("note_note") or ""

        cur.execute(
            "SELECT pseudonym FROM pseudonyms WHERE author_id = %s",
            (author_id,),
        )
        pseudonym_ids = [r["pseudonym"] for r in cur.fetchall()]

        cur.execute(
            """
            SELECT DISTINCT t.title_id, t.title_title, t.title_copyright
            FROM canonical_author ca
            JOIN titles t ON t.title_id = ca.title_id
            WHERE ca.author_id = %s AND t.title_ttype IN %s
            ORDER BY t.title_copyright ASC
            """,
            (author_id, BOOK_TTYPES),
        )
        works = []
        for t in cur.fetchall():
            cur.execute(
                """
                SELECT p.pub_isbn, p.pub_frontimage FROM pub_content pc
                JOIN pubs p ON p.pub_id = pc.pub_id
                WHERE pc.title_id = %s
                ORDER BY p.pub_year ASC
                LIMIT 1
                """,
                (t["title_id"],),
            )
            pub = cur.fetchone() or {}
            isbn = clean_isbn(pub.get("pub_isbn") or "")
            isbn10 = isbn if len(isbn) == 10 else (isbn13_to_10(isbn) if len(isbn) == 13 else None)
            isbn13 = isbn if len(isbn) == 13 else (isbn10_to_13(isbn) if len(isbn) == 10 else None)
            works.append({
                "title": t["title_title"],
                "isbn_13": isbn13 or "",
                "isbn_10": isbn10 or "",
                "publish_year": date_year(t.get("title_copyright")),
                "cover_url": pub.get("pub_frontimage") or "",
            })

        return {
            "provider": "isfdb",
            "external_id": str(author_id),
            "name": author["author_canonical"],
            "legal_name": author.get("author_legalname") or "",
            "bio": bio,
            "born_date": date_str(author.get("author_birthdate")) or None,
            "died_date": date_str(author.get("author_deathdate")) or None,
            "nationality": "",
            "photo_url": author.get("author_image") or "",
            "pseudonym_ids": [str(p) for p in pseudonym_ids],
            "works": works,
        }


@app.exception_handler(pymysql.MySQLError)
def db_error_handler(request, exc):
    return JSONResponse(status_code=503, content={"error": f"database error: {exc}"})
