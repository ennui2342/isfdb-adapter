# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Mark Cheverton

"""ISFDB mirror adapter — thin JSON API in front of a local ISFDB MySQL/MariaDB
mirror. Originally built as the backend for Librarium's ISFDB metadata
provider, but generic enough for any consumer that wants ISFDB data.

Endpoints:
  GET /isbn/{isbn}                book lookup by ISBN-10 or ISBN-13 (most
                                   recent printing sharing the ISBN, if
                                   more than one); ?all=true returns every
                                   matching printing instead of just one
  GET /isbn/{isbn}/editions       every publication of the same work as
                                   this ISBN, regardless of their own ISBN
  GET /search?q=                  freetext title/author search
  GET /series/search?q=           freetext series-name search
  GET /series/{series_id}/volumes ordered per-volume list for a series
  GET /pubseries/{id}             a publisher's line (ISFDB publication
                                   series) and every printing in it
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

# Book-length title types. ANTHOLOGY and NONFICTION are books too — leaving
# them out meant an anthology's ISBN came back with no title id, its
# printings couldn't be listed, and search never found it (opsimath,
# Continuum 4, 2026-11-08). MAGAZINE/FANZINE issues are periodicals, not
# books, and stay out.
BOOK_TTYPES = ("NOVEL", "COLLECTION", "OMNIBUS", "CHAPBOOK", "ANTHOLOGY", "NONFICTION")

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


NO_TABLE = 1146  # MariaDB ER_NO_SUCH_TABLE


def pub_series_name(cur, pub_series_id):
    """A publication series' name — or None when the mirror predates
    importing the pub_series table (refresh.py's WANTED_TABLES gained it
    2026-11-08): a deploy that lands before the next re-import must not
    break every result (it 503'd /search once)."""
    try:
        cur.execute("SELECT pub_series_name FROM pub_series WHERE pub_series_id = %s", (pub_series_id,))
    except pymysql.err.ProgrammingError as e:
        if e.args and e.args[0] == NO_TABLE:
            return None
        raise
    row = cur.fetchone()
    return row["pub_series_name"] if row else None


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

    # Authors with their ISFDB ids (parallel lists), so a consumer can tie
    # its own author records to ISFDB's through the books, not by name.
    authors: list[str] = []
    author_ids: list[int] = []
    if title_row:
        cur.execute(
            """
            SELECT a.author_id, a.author_canonical
            FROM canonical_author ca
            JOIN authors a ON a.author_id = ca.author_id
            WHERE ca.title_id = %s
            """,
            (title_row["title_id"],),
        )
        rows = cur.fetchall()
        authors, author_ids = [r["author_canonical"] for r in rows], [r["author_id"] for r in rows]
    if not authors:
        cur.execute(
            """
            SELECT a.author_id, a.author_canonical
            FROM pub_authors pa
            JOIN authors a ON a.author_id = pa.author_id
            WHERE pa.pub_id = %s
            """,
            (pub["pub_id"],),
        )
        rows = cur.fetchall()
        authors, author_ids = [r["author_canonical"] for r in rows], [r["author_id"] for r in rows]

    # Cover art credit, genuinely edition-level (unlike everything else
    # here, which comes off the shared title record): ISFDB tracks it as
    # its own COVERART title linked to this exact publication via
    # pub_content, with its own canonical_author(s) — a printing can be
    # re-illustrated on reissue while the underlying NOVEL title (and its
    # authors, above) stays the same.
    cur.execute(
        """
        SELECT a.author_canonical
        FROM pub_content pc
        JOIN titles t ON t.title_id = pc.title_id
        JOIN canonical_author ca ON ca.title_id = t.title_id
        JOIN authors a ON a.author_id = ca.author_id
        WHERE pc.pub_id = %s AND t.title_ttype = 'COVERART'
        """,
        (pub["pub_id"],),
    )
    cover_artists = [r["author_canonical"] for r in cur.fetchall()]

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

    # The publisher's branded line this printing belongs to — ISFDB's
    # *publication series* (The Women's Press Science Fiction, SF
    # Masterworks), distinct from the title's story series above and from
    # the publisher itself — with the printing's number in the line.
    pub_series = pub_series_name(cur, pub.get("pub_series_id")) if pub.get("pub_series_id") else None

    # The title's story series by name (The Snow Queen Cycle), beside its
    # id and number below — so a consumer can file the book in the series
    # without a further call.
    series_name = None
    if title_row and title_row.get("series_id"):
        cur.execute("SELECT series_title FROM series WHERE series_id = %s", (title_row["series_id"],))
        srow = cur.fetchone()
        series_name = srow["series_title"] if srow else None

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
        "_isfdb_author_ids": author_ids,
        "cover_artists": cover_artists,
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
        "series": series_name or "",
        "pub_series": pub_series or "",
        "pub_series_num": (pub.get("pub_series_num") or "").strip(),
        "_isfdb_pub_series_id": pub.get("pub_series_id") or None,
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/isbn/{isbn}")
def lookup_isbn(isbn: str, all: bool = False):
    # Query pub_isbn directly (unwrapped) so MySQL can use its existing
    # index. An earlier version wrapped it in REPLACE(REPLACE(...)) to
    # tolerate hyphens/spaces "just in case" — that defensive wrapping
    # disables the index and forces a full ~950K-row scan on every lookup
    # (took 2.5+ minutes cold in production). ISFDB's pub_isbn is verified
    # clean (checked: 0 of 664044 non-empty values contain a hyphen or
    # space), so candidates are normalized client-side instead.
    #
    # An ISBN isn't always unique to one pub: confirmed against real data
    # (2026-08-10) that 28,102 of 626,332 distinct pub_isbn values in the
    # mirror belong to more than one pub — and for opsimath's own library
    # specifically, 565 of 1,493 ISBNs matched at all (~38%) hit this,
    # skewed high by how often vintage mass-market SF gets reprinted under
    # the same ISBN. Default behavior (all=False, unchanged from before
    # this comment — librarium's own existing integration depends on this
    # single-object shape) picks the most recent printing sharing the
    # ISBN, same as always. `all=True` is additive: returns every
    # candidate instead, so a caller that already has its own signal for
    # which specific printing it means (e.g. a previously-recorded
    # publish year) can pick the right one itself rather than trusting a
    # blind "latest wins" default — see opsimath's Enrichment::IsfdbEditionEnricher.
    candidates = isbn_candidates(isbn)
    with db() as conn, conn.cursor() as cur:
        placeholders = ", ".join(["%s"] * len(candidates))
        if all:
            cur.execute(
                f"SELECT * FROM pubs WHERE pub_isbn IN ({placeholders}) "
                f"ORDER BY pub_year DESC",
                candidates,
            )
            pubs = cur.fetchall()
            if not pubs:
                raise HTTPException(status_code=404, detail="isbn not found")
            return [book_result_from_pub(cur, pub) for pub in pubs]

        cur.execute(
            f"SELECT * FROM pubs WHERE pub_isbn IN ({placeholders}) "
            f"ORDER BY pub_year DESC LIMIT 1",
            candidates,
        )
        pub = cur.fetchone()
        if not pub:
            raise HTTPException(status_code=404, detail="isbn not found")
        return book_result_from_pub(cur, pub)


def title_family(cur, title_ids):
    """The given titles plus their variant family, in the same language.

    ISFDB records a book reissued under a different title, author credit or
    edition (Gollancz's SF Masterworks *The Lathe of Heaven*, title 1765551)
    as a *variant* of a parent title (7661), and hangs each printing off
    whichever of the two it carries. Following only the ISBN's own title
    returned the 10 Masterworks printings and none of the parent's 39 — no
    Avon or Panther paperbacks. So: each title's root (its parent, or
    itself), then every book-type title under that root in the starting
    title's language — a translation is a variant too, and an English ISBN
    shouldn't bring back German printings (nor a German one English).
    """
    placeholders = ", ".join(["%s"] * len(title_ids))
    cur.execute(
        f"""
        SELECT title_id, IF(title_parent > 0, title_parent, title_id) AS root, title_language
        FROM titles WHERE title_id IN ({placeholders})
        """,
        title_ids,
    )
    family = set(title_ids)
    for start in cur.fetchall():
        cur.execute(
            """
            SELECT title_id FROM titles
            WHERE (title_id = %s OR title_parent = %s) AND title_ttype IN %s AND title_language <=> %s
            """,
            (start["root"], start["root"], BOOK_TTYPES, start["title_language"]),
        )
        family.update(r["title_id"] for r in cur.fetchall())
    return sorted(family)


def isbn10_check_digit(first9: str) -> str:
    total = sum((10 - i) * int(d) for i, d in enumerate(first9))
    check = (11 - total % 11) % 11
    return "X" if check == 10 else str(check)


@app.get("/isbn/middle/{digits}")
def isbns_with_middle(digits: str, limit: int = 100):
    """Printings whose ISBN-10 has `digits` as digits 5-9: what a US
    mass-market paperback's 5-digit UPC add-on encodes. (Its UPC itself
    names only the publisher and cover price, shared by every book at that
    price.) English-language ISBNs start with group 0 or 1, so there are
    only 2,000 possible 4-digit prefixes: every complete ISBN-10 and its
    978 ISBN-13 is built here and looked up through the pub_isbn index
    (a pattern match on the middle digits would scan every row)."""
    if not re.fullmatch(r"\d{5}", digits):
        raise HTTPException(status_code=400, detail="five digits expected")
    candidates = []
    for prefix in range(2000):
        first9 = f"{prefix:04d}{digits}"
        isbn10 = first9 + isbn10_check_digit(first9)
        candidates += [isbn10, isbn10_to_13(isbn10)]
    with db() as conn, conn.cursor() as cur:
        placeholders = ",".join(["%s"] * len(candidates))
        cur.execute(f"SELECT * FROM pubs WHERE pub_isbn IN ({placeholders}) LIMIT %s", (*candidates, limit))
        return [book_result_from_pub(cur, pub) for pub in cur.fetchall()]


# English-group ISBN-10 publisher ranges (group digit, low, high, code
# length): a publisher's code length is fixed by its range, so the title
# part is what's left of the nine digits.
ENGLISH_PUBLISHER_RANGES = [
    ("0", 200, 699, 3), ("0", 7000, 8499, 4), ("0", 85000, 89999, 5),
    ("1", 100, 399, 3), ("1", 4000, 5499, 4),
]


def addon_isbn10s(digits: str) -> list[str]:
    """Every ISBN-10 a US mass-market UPC add-on can stand for. Publishers
    use one of two conventions: the add-on is ISBN digits 5-9 (Tor,
    0-8125-5010-2: 55010), or the ISBN's title part zero-padded to five
    digits (DAW, 0-88677-299-0: 00299) — the title part's length fixed by
    the publisher code's range."""
    out = []
    for prefix in range(2000):  # digits 5-9, any English-group 4-digit start
        first9 = f"{prefix:04d}{digits}"
        out.append(first9 + isbn10_check_digit(first9))
    for group, low, high, code_len in ENGLISH_PUBLISHER_RANGES:
        title_len = 9 - 1 - code_len
        if title_len > 5 or digits[: 5 - title_len].strip("0"):
            continue
        title = digits[5 - title_len:]
        for code in range(low, high + 1):
            first9 = f"{group}{code:0{code_len}d}{title}"
            out.append(first9 + isbn10_check_digit(first9))
    return list(dict.fromkeys(out))


@app.get("/isbn/addon/{digits}")
def isbns_for_addon(digits: str, limit: int = 100):
    """Printings a US mass-market UPC add-on can identify (addon_isbn10s),
    looked up through the pub_isbn index — supersedes /isbn/middle, which
    assumed a 3-digit publisher code."""
    if not re.fullmatch(r"\d{5}", digits):
        raise HTTPException(status_code=400, detail="five digits expected")
    isbn10s = addon_isbn10s(digits)
    candidates = isbn10s + [isbn10_to_13(i) for i in isbn10s]
    with db() as conn, conn.cursor() as cur:
        rows = []
        for i in range(0, len(candidates), 5000):
            chunk = candidates[i : i + 5000]
            placeholders = ",".join(["%s"] * len(chunk))
            cur.execute(f"SELECT * FROM pubs WHERE pub_isbn IN ({placeholders})", chunk)
            rows += cur.fetchall()
            if len(rows) >= limit:
                break
        return [book_result_from_pub(cur, pub) for pub in rows[:limit]]


@app.get("/isbn/{isbn}/editions")
def lookup_isbn_editions(isbn: str):
    """Given an ISBN, resolve it to its title(s) (a reused ISBN can
    legitimately belong to more than one — no assumption made here that
    it's exactly one) and return every publication under all of them —
    i.e. every other edition/printing/ISBN of the same work, not just
    ones sharing this exact ISBN. Distinct from `?all=true` above: that
    stays scoped to "other pubs with this same ISBN" (the reused-ISBN
    case), this is "every pub of the same title regardless of its own
    ISBN" — the broader candidate set a caller matching by something
    other than ISBN (e.g. a cover image) actually needs. Same per-title
    query /search already uses per matched title, just entered via a
    known ISBN instead of a fuzzy text match — exact and index-backed,
    not a FULLTEXT score to compute.

    A variant title brings its family: the parent and its other
    same-language variants (see title_family).
    """
    candidates = isbn_candidates(isbn)
    with db() as conn, conn.cursor() as cur:
        placeholders = ", ".join(["%s"] * len(candidates))
        cur.execute(f"SELECT pub_id FROM pubs WHERE pub_isbn IN ({placeholders})", candidates)
        pub_ids = [r["pub_id"] for r in cur.fetchall()]
        if not pub_ids:
            raise HTTPException(status_code=404, detail="isbn not found")

        pub_placeholders = ", ".join(["%s"] * len(pub_ids))
        cur.execute(
            f"""
            SELECT DISTINCT t.title_id FROM pub_content pc
            JOIN titles t ON t.title_id = pc.title_id
            WHERE pc.pub_id IN ({pub_placeholders}) AND t.title_ttype IN %s
            """,
            (*pub_ids, BOOK_TTYPES),
        )
        title_ids = [r["title_id"] for r in cur.fetchall()]
        if not title_ids:
            raise HTTPException(status_code=404, detail="isbn found but not linked to a book-type title")

        title_ids = title_family(cur, title_ids)
        title_placeholders = ", ".join(["%s"] * len(title_ids))
        cur.execute(
            f"""
            SELECT DISTINCT p.* FROM pub_content pc
            JOIN pubs p ON p.pub_id = pc.pub_id
            WHERE pc.title_id IN ({title_placeholders})
            ORDER BY (p.pub_year IS NULL OR p.pub_year IN ('0000-00-00', '8888-00-00')) ASC, p.pub_year ASC
            """,
            title_ids,
        )
        pubs = cur.fetchall()
        return [book_result_from_pub(cur, pub) for pub in pubs]


@app.get("/titles/{title_id}/editions")
def title_editions(title_id: int):
    """Every publication of a title and its same-language variant family
    (title_family) — /isbn/{isbn}/editions entered by a known title id
    instead of an ISBN. A consumer that already knows which title a book is
    needs this: the only ISBN it has can belong to a different title (The
    Midwich Cuckoos' only ISBN was a schools chapbook's, title 172997, so
    the novel's printings, title 7701, were never listed)."""
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT title_id FROM titles WHERE title_id = %s", (title_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="title not found")
        title_ids = title_family(cur, [title_id])
        placeholders = ", ".join(["%s"] * len(title_ids))
        cur.execute(
            f"""
            SELECT DISTINCT p.* FROM pub_content pc
            JOIN pubs p ON p.pub_id = pc.pub_id
            WHERE pc.title_id IN ({placeholders})
            ORDER BY (p.pub_year IS NULL OR p.pub_year IN ('0000-00-00', '8888-00-00')) ASC, p.pub_year ASC
            """,
            title_ids,
        )
        return [book_result_from_pub(cur, pub) for pub in cur.fetchall()]


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
            SELECT t.title_id, t.title_title, t.title_seriesnum, t.title_copyright, l.lang_code
            FROM titles t
            LEFT JOIN languages l ON l.lang_id = t.title_language
            WHERE t.series_id = %s AND t.title_ttype IN %s
            ORDER BY t.title_seriesnum IS NULL, t.title_seriesnum ASC, t.title_copyright ASC
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
            cur.execute(
                """
                SELECT a.author_canonical FROM canonical_author ca
                JOIN authors a ON a.author_id = ca.author_id
                WHERE ca.title_id = %s
                """,
                (t["title_id"],),
            )
            authors = [r["author_canonical"] for r in cur.fetchall()]
            out.append({
                "position": float(t["title_seriesnum"]) if t["title_seriesnum"] is not None else 0.0,
                "title": t["title_title"],
                "release_date": date_str(t.get("title_copyright")),
                "cover_url": (cover or {}).get("pub_frontimage") or "",
                "external_id": str(t["title_id"]),
                # Added 2026-11-08 for opsimath's series pages: a series can
                # hold a translation's own omnibus (Le cycle de Tiamat).
                "language": t.get("lang_code") or "",
                "authors": authors,
            })
        return out


@app.get("/titles/{title_id}")
def fetch_title(title_id: int):
    """A title's main title and every variant of it — the main (parent)
    title is ISFDB's canonical one, generally the title of first
    publication; variants are later retitlings, other credits and
    translations. Lets a consumer tell a book known by another title (Extro
    is a variant of The Computer Connection) from one linked to the wrong
    record."""
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT title_id, title_title, title_parent, title_ttype FROM titles WHERE title_id = %s", (title_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="title not found")
        main_id = row["title_parent"] or row["title_id"]
        cur.execute(
            """
            SELECT t.title_id, t.title_title, t.title_ttype, l.lang_code
            FROM titles t LEFT JOIN languages l ON l.lang_id = t.title_language
            WHERE t.title_id = %s OR t.title_parent = %s
            """,
            (main_id, main_id),
        )
        rows = cur.fetchall()
        main = next((r for r in rows if r["title_id"] == main_id), None)
        return {
            "id": title_id,
            "title": row["title_title"],
            "type": row["title_ttype"],
            "main": {"id": main_id, "title": (main or {}).get("title_title") or row["title_title"],
                     "language": (main or {}).get("lang_code") or ""},
            "variants": [{"id": r["title_id"], "title": r["title_title"], "language": r.get("lang_code") or ""}
                         for r in rows if r["title_id"] != main_id],
        }


@app.get("/pubseries/{pub_series_id}")
def publication_series(pub_series_id: int):
    """A publisher's line (ISFDB publication series) and every printing in
    it, in line order — numbered ones by number, then the rest by date."""
    with db() as conn, conn.cursor() as cur:
        try:
            cur.execute("SELECT pub_series_id, pub_series_name FROM pub_series WHERE pub_series_id = %s", (pub_series_id,))
        except pymysql.err.ProgrammingError as e:
            if e.args and e.args[0] == NO_TABLE:
                raise HTTPException(status_code=503, detail="publication series not imported yet — awaiting the next mirror refresh")
            raise
        series = cur.fetchone()
        if not series:
            raise HTTPException(status_code=404, detail="publication series not found")
        cur.execute(
            """
            SELECT * FROM pubs WHERE pub_series_id = %s
            ORDER BY (pub_series_num IS NULL OR pub_series_num = '') ASC,
                     CAST(pub_series_num AS UNSIGNED) ASC, pub_series_num ASC, pub_year ASC
            """,
            (pub_series_id,),
        )
        return {
            "id": series["pub_series_id"],
            "name": series["pub_series_name"],
            "publications": [book_result_from_pub(cur, pub) for pub in cur.fetchall()],
        }


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
def fetch_author(author_id: int, works: bool = True):
    # works=false skips the bibliography — one query per title, so ~27s
    # for a prolific author (Asimov, 768 works) — for callers that only
    # need the profile itself (e.g. opsimath's living/dead check).
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

        titles = []
        if works:
            cur.execute(
                """
                SELECT DISTINCT t.title_id, t.title_title, t.title_copyright, t.title_ttype, t.title_parent,
                       t.series_id, t.title_seriesnum, s.series_title, l.lang_code
                FROM canonical_author ca
                JOIN titles t ON t.title_id = ca.title_id
                LEFT JOIN series s ON s.series_id = t.series_id
                LEFT JOIN languages l ON l.lang_id = t.title_language
                WHERE ca.author_id = %s AND t.title_ttype IN %s
                ORDER BY t.title_copyright ASC
                """,
                (author_id, BOOK_TTYPES),
            )
            titles = cur.fetchall()
        works_out = []
        for t in titles:
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
            works_out.append({
                "title": t["title_title"],
                "isbn_13": isbn13 or "",
                "isbn_10": isbn10 or "",
                "publish_year": date_year(t.get("title_copyright")),
                "cover_url": pub.get("pub_frontimage") or "",
                # Added 2026-11-08 for opsimath's author pages: which title,
                # what kind (NOVEL, OMNIBUS…), its language, the title it's
                # a variant of (a retitling or translation; null if none),
                # and its series.
                "_isfdb_title_id": t["title_id"],
                "type": t.get("title_ttype") or "",
                "language": t.get("lang_code") or "",
                "variant_of": t.get("title_parent") or None,
                "_isfdb_series_id": t.get("series_id"),
                "_isfdb_series_num": t.get("title_seriesnum"),
                "series": t.get("series_title") or "",
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
            "works": works_out,
        }


@app.exception_handler(pymysql.MySQLError)
def db_error_handler(request, exc):
    return JSONResponse(status_code=503, content={"error": f"database error: {exc}"})
