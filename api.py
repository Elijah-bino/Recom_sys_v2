from __future__ import annotations

import json
import os
import secrets
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from jose import JWTError
from pydantic import BaseModel, Field

from recommender.data import Catalog, load_catalog, resolve_seed_items
from recommender.model import Recommender
from recommender.recency_rank import apply_recency_rerank, recency_pool_k
from recommender.tmdb import TMDBTrailerResolver
from recommender.youtube import YouTubeTrailerResolver
from server.auth import auth_config, create_token, decode_token, hash_password, verify_password
from server.db import Db, create_user, fetch_run_by_slug, get_user_by_email, get_user_by_id, init_db, insert_rec_run, list_liked_imdb


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name, default) or default).strip()


REPO_ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("DB_PATH", str(REPO_ROOT / ".data" / "recomsys.db")))


class Filters(BaseModel):
    kind: Literal["movie", "series"] | None = None
    year_min: int | None = None
    year_max: int | None = None
    runtime_min: int | None = None
    runtime_max: int | None = None
    certificate: str | None = None
    genres: list[str] | None = None


class QueryReq(BaseModel):
    query: str = Field(..., min_length=1)
    top_k: int = Field(5, ge=1, le=50)
    filters: Filters | None = None
    # When true (default), re-rank embedding candidates so newer releases surface first.
    prefer_recent: bool = True


class LikedReq(BaseModel):
    liked_imdb: list[str] = Field(default_factory=list)
    liked_title: list[str] = Field(default_factory=list)
    top_k: int = Field(5, ge=1, le=50)
    filters: Filters | None = None
    prefer_recent: bool = True


class RegisterBody(BaseModel):
    email: str = Field(..., min_length=5, max_length=200)
    password: str = Field(..., min_length=6, max_length=200)


class LoginBody(BaseModel):
    email: str = Field(..., min_length=5, max_length=200)
    password: str = Field(..., min_length=6, max_length=200)


class AuthResponse(BaseModel):
    token: str
    email: str


class MeResponse(BaseModel):
    email: str
    liked_imdb: list[str] = Field(default_factory=list)


def _apply_filters(df, f: Filters | None):
    if not f:
        return df

    out = df

    # Optional columns depending on dataset (be defensive).
    if f.kind and "kind" in out.columns:
        out = out[out["kind"].astype(str).str.lower() == f.kind]

    if f.certificate and "certificate" in out.columns:
        out = out[out["certificate"].astype(str) == f.certificate]

    if f.year_min is not None and "release_year" in out.columns:
        out = out[out["release_year"].fillna(-1).astype(int) >= int(f.year_min)]
    if f.year_max is not None and "release_year" in out.columns:
        out = out[out["release_year"].fillna(10**9).astype(int) <= int(f.year_max)]

    if f.runtime_min is not None and "runtime" in out.columns:
        out = out[out["runtime"].fillna(-1).astype(int) >= int(f.runtime_min)]
    if f.runtime_max is not None and "runtime" in out.columns:
        out = out[out["runtime"].fillna(10**9).astype(int) <= int(f.runtime_max)]

    if f.genres and "genre" in out.columns:
        wanted = {g.strip() for g in f.genres if str(g).strip()}
        if wanted:
            # keep rows where any wanted genre appears in the comma-separated genre string
            gseries = out["genre"].fillna("").astype(str)
            mask = gseries.apply(lambda s: any(w in {x.strip() for x in s.split(",")} for w in wanted))
            out = out[mask]

    return out


def _year_for_trailer(y: Any) -> str | None:
    """TMDB year hint; catalog may store int, float (2011.0), or NaN."""
    if y is None:
        return None
    try:
        if isinstance(y, float) and pd.isna(y):
            return None
    except Exception:
        pass
    try:
        yi = int(float(y))
    except (TypeError, ValueError):
        return None
    return str(yi) if 1800 <= yi <= 2100 else None


def _row_to_result(df_row: dict[str, Any], score: float, yt: YouTubeTrailerResolver) -> dict[str, Any]:
    title = str(df_row.get("title") or "").strip()
    imdb_id = str(df_row.get("imdb_id") or "").strip()
    genre = str(df_row.get("genre") or "").strip()
    synopsis = str(df_row.get("synopsis") or "").strip()
    year_s = _year_for_trailer(df_row.get("release_year"))

    # TMDB-merged catalog uses tmdb_media_type; legacy rows may use kind
    tm = str(df_row.get("tmdb_media_type") or "").strip().lower()
    kind_raw = str(df_row.get("kind") or "").strip().lower()
    if tm in {"movie", "tv"}:
        media_kind: Literal["movie", "tv"] = tm  # type: ignore[assignment]
    elif kind_raw in {"series", "tv", "show"}:
        media_kind = "tv"
    else:
        media_kind = "movie"
    hit = yt.resolve(movie_title=title, year=year_s, kind=media_kind)  # type: ignore[arg-type]
    video_id = hit.video_id if hit else ""
    watch_url = yt.watch_url(video_id) if video_id else ""
    search_url = yt.search_url_fallback(f"{title} trailer") if title else ""

    return {
        "title": title,
        "imdb_id": imdb_id,
        "genre": genre,
        "synopsis": synopsis,
        "score": float(score),
        "youtube_video_id": video_id,
        "youtube_watch_url": watch_url,
        "youtube_search_url": search_url,
    }


@lru_cache(maxsize=1)
def _catalog() -> Catalog:
    catalog_path = _env("CATALOG_PATH", "catalog_meta.csv")
    limit_rows = _env("LIMIT_ROWS", "")
    lim = int(limit_rows) if limit_rows.isdigit() else None
    return load_catalog(catalog_path, limit_rows=lim)


@lru_cache(maxsize=1)
def _recommender() -> Recommender:
    c = _catalog()
    return Recommender(catalog=c)


@lru_cache(maxsize=1)
def _item_matrix() -> np.ndarray:
    cache_path = _env("CACHE_PATH", ".cache/item_matrix.npy")
    force = _env("FORCE_REBUILD_MATRIX", "").lower() in {"1", "true", "yes"}

    rec = _recommender()
    return rec.build_item_matrix(cache_path=cache_path, force_rebuild=force)


@lru_cache(maxsize=1)
def _yt() -> YouTubeTrailerResolver:
    return YouTubeTrailerResolver()


def _try_user(authorization: str | None = Header(default=None)) -> tuple[int, str] | None:
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        return None
    try:
        return decode_token(token)
    except JWTError:
        return None


def _require_user(authorization: str | None = Header(default=None)) -> tuple[int, str]:
    u = _try_user(authorization)
    if u is None:
        raise HTTPException(status_code=401, detail="Missing or invalid token")
    return u


def _filters_dict(f: Filters | None) -> dict[str, Any] | None:
    if f is None:
        return None
    return json.loads(f.model_dump_json())


def _new_share_slug() -> str:
    return secrets.token_urlsafe(12)


def _save_share(
    conn: sqlite3.Connection,
    *,
    user_id: int | None,
    mode: str,
    query: str | None,
    filters: Filters | None,
    results: list[dict[str, Any]],
) -> str:
    slug = _new_share_slug()
    ts = datetime.now(timezone.utc).isoformat()
    for _ in range(5):
        try:
            insert_rec_run(
                conn,
                user_id=user_id,
                mode=mode,
                query=query,
                filters=_filters_dict(filters),
                results=results,
                share_slug=slug,
                created_at=ts,
            )
            return slug
        except sqlite3.IntegrityError:
            slug = _new_share_slug()
    raise HTTPException(status_code=500, detail="Could not allocate share slug")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    db = Db(DB_PATH)
    conn = db.connect()
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(conn)
    app.state.db_conn = conn
    yield
    conn.close()


app = FastAPI(title="RecomSys API", version="0.1.0", lifespan=_lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Serve static vanilla JS UI at /ui (optional)
_frontend_dir = Path(__file__).parent / "frontend"
if _frontend_dir.exists():
    app.mount("/ui", StaticFiles(directory=str(_frontend_dir), html=True), name="ui")


@app.get("/health")
def health() -> dict[str, Any]:
    yt = _yt()
    tmdb = TMDBTrailerResolver()
    # Lazy: matrix is only built when needed (or when this endpoint is called).
    try:
        mat = _item_matrix()
        matrix_ok = bool(mat is not None and getattr(mat, "shape", None))
        d = int(mat.shape[1]) if matrix_ok else None
    except Exception:
        matrix_ok = False
        d = None

    return {
        "ok": True,
        "catalog_rows": int(len(_catalog().df)),
        "matrix_ready": matrix_ok,
        "matrix_dim": d,
        "tmdb_api_configured": tmdb.enabled(),
        "youtube_api_configured": yt.enabled(),
        "youtube_quota_exceeded": bool(getattr(yt, "quota_exceeded", False)),
        "ui_mounted": _frontend_dir.exists(),
        "db_path": str(DB_PATH),
    }


@app.post("/auth/register", response_model=AuthResponse)
def auth_register(body: RegisterBody, request: Request) -> AuthResponse:
    conn = request.app.state.db_conn
    row = get_user_by_email(conn, body.email)
    if row is not None:
        raise HTTPException(status_code=409, detail="Email already registered")
    ph = hash_password(body.password)
    ts = datetime.now(timezone.utc).isoformat()
    try:
        uid = create_user(conn, email=body.email, password_hash=ph, created_at=ts)
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="Email already registered") from None
    tok = create_token(user_id=uid, email=body.email.strip())
    return AuthResponse(token=tok, email=body.email.strip())


@app.post("/auth/login", response_model=AuthResponse)
def auth_login(body: LoginBody, request: Request) -> AuthResponse:
    conn = request.app.state.db_conn
    row = get_user_by_email(conn, body.email)
    if row is None or not verify_password(body.password, str(row["password_hash"])):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    uid = int(row["id"])
    email = str(row["email"])
    tok = create_token(user_id=uid, email=email)
    return AuthResponse(token=tok, email=email)


@app.get("/me", response_model=MeResponse)
def auth_me(request: Request, user: tuple[int, str] = Depends(_require_user)) -> MeResponse:
    uid, email = user
    conn = request.app.state.db_conn
    row = get_user_by_id(conn, uid)
    if row is None:
        raise HTTPException(status_code=401, detail="User not found")
    liked = list_liked_imdb(conn, uid)
    return MeResponse(email=str(row["email"]), liked_imdb=liked)


@app.get("/share/{slug}")
def get_share(slug: str, request: Request) -> dict[str, Any]:
    conn = request.app.state.db_conn
    run = fetch_run_by_slug(conn, slug)
    if run is None:
        raise HTTPException(status_code=404, detail="Share not found")
    return {"slug": slug, "rec_run_id": int(run["id"]), "results": run.get("results") or []}


@app.get("/search")
def search(q: str = Query("", min_length=0), limit: int = Query(20, ge=1, le=50)) -> dict[str, Any]:
    df = _catalog().df
    qq = (q or "").strip().lower()
    if not qq:
        return {"results": []}
    # simple contains search; OK for MVP (catalog can be big — keep limit small)
    m = df["title"].astype(str).str.lower().str.contains(qq, na=False)
    hit = df.loc[m, ["title", "imdb_id", "genre"]].head(int(limit))
    return {"results": hit.to_dict(orient="records")}


@app.get("/filters/options")
def filter_options() -> dict[str, Any]:
    df = _catalog().df
    if "release_year" in df.columns:
        y = pd.to_numeric(df["release_year"], errors="coerce")
        years = sorted({int(v) for v in y.dropna().astype(int).tolist()})
    else:
        years = []
    genres = sorted({g.strip() for s in df.get("genre", []).fillna("").astype(str).tolist() for g in s.split(",") if g.strip()}) if "genre" in df.columns else []
    certs = sorted({str(x).strip() for x in df.get("certificate", []).dropna().astype(str).tolist() if str(x).strip()}) if "certificate" in df.columns else []
    return {"years": years, "genres": genres, "certificates": certs}


@app.post("/recommend/query")
def recommend_query(
    req: QueryReq,
    request: Request,
    user: tuple[int, str] | None = Depends(_try_user),
) -> dict[str, Any]:
    c = _catalog()
    rec = _recommender()
    mat = _item_matrix()

    df = c.df
    if req.filters:
        df2 = _apply_filters(df, req.filters)
    else:
        df2 = df

    pool_k = recency_pool_k(req.top_k, len(df2))

    if len(df2) != len(df):
        # Build a view matrix for the filtered subset
        idx_map = df2.index.to_numpy(dtype=np.int64)
        mat2 = mat[idx_map]
        idx2, scores = rec.recommend_from_query(req.query, top_k=pool_k, item_matrix=mat2)
        picked = idx_map[idx2]
    else:
        idx2, scores = rec.recommend_from_query(req.query, top_k=pool_k, item_matrix=mat)
        picked = idx2

    year_active = bool(req.filters and (req.filters.year_min is not None or req.filters.year_max is not None))
    picked, scores = apply_recency_rerank(
        df,
        picked,
        scores,
        top_k=req.top_k,
        query=req.query,
        year_filter_active=year_active,
        prefer_recent=req.prefer_recent,
    )

    yt = _yt()
    results = [_row_to_result(df.loc[int(i)].to_dict(), float(s), yt) for i, s in zip(picked.tolist(), scores.tolist())]
    share_slug: str | None = None
    if user is not None:
        uid, _email = user
        conn = request.app.state.db_conn
        share_slug = _save_share(
            conn,
            user_id=uid,
            mode="query",
            query=req.query,
            filters=req.filters,
            results=results,
        )
    return {"results": results, "share_slug": share_slug}


@app.post("/recommend/liked")
def recommend_liked(
    req: LikedReq,
    request: Request,
    user: tuple[int, str] | None = Depends(_try_user),
) -> dict[str, Any]:
    c = _catalog()
    rec = _recommender()
    mat = _item_matrix()

    liked_idx = resolve_seed_items(c, liked_imdb_ids=req.liked_imdb, liked_titles=req.liked_title)
    if liked_idx.size == 0:
        return {"results": [], "share_slug": None}

    df = c.df
    if req.filters:
        df2 = _apply_filters(df, req.filters)
    else:
        df2 = df

    pool_k = recency_pool_k(req.top_k, len(df2))

    if len(df2) != len(df):
        idx_map = df2.index.to_numpy(dtype=np.int64)
        # liked indices might be outside filter set; intersect.
        liked_set = set(map(int, liked_idx.tolist()))
        liked_in = np.array([i for i in idx_map.tolist() if int(i) in liked_set], dtype=np.int64)
        if liked_in.size == 0:
            return {"results": [], "share_slug": None}
        mat2 = mat[idx_map]
        liked_pos = np.array([int(np.where(idx_map == i)[0][0]) for i in liked_in.tolist()], dtype=np.int64)
        idx2, scores = rec.recommend_from_liked_items(liked_pos, top_k=pool_k, item_matrix=mat2)
        picked = idx_map[idx2]
    else:
        idx2, scores = rec.recommend_from_liked_items(liked_idx, top_k=pool_k, item_matrix=mat)
        picked = idx2

    year_active = bool(req.filters and (req.filters.year_min is not None or req.filters.year_max is not None))
    picked, scores = apply_recency_rerank(
        df,
        picked,
        scores,
        top_k=req.top_k,
        query=None,
        year_filter_active=year_active,
        prefer_recent=req.prefer_recent,
    )

    yt = _yt()
    results = [_row_to_result(df.loc[int(i)].to_dict(), float(s), yt) for i, s in zip(picked.tolist(), scores.tolist())]
    share_slug: str | None = None
    if user is not None:
        uid, _email = user
        conn = request.app.state.db_conn
        share_slug = _save_share(
            conn,
            user_id=uid,
            mode="liked",
            query=None,
            filters=req.filters,
            results=results,
        )
    return {"results": results, "share_slug": share_slug}
