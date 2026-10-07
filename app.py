# 小工具：取目前登入者（同步）
from fastapi import Request

def current_user(request: Request):
    return request.session.get("user")  # {id, username, role} 或 None

# === app.py ===
import os
import uuid
from datetime import datetime, timedelta
from pathlib import Path
import re

from fastapi import FastAPI, Form, UploadFile, File, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, PlainTextResponse
from starlette.middleware.sessions import SessionMiddleware
from psycopg.rows import dict_row
from datetime import timezone

from db import get_conn
import psycopg

REVIEW_WINDOW_DAYS = 7

# ----------------
# 查評價
# ----------------
def fetch_user_review_summary(user_id: int, role: str, limit_comments: int = 5):
    """
    role:
      - "client"      -> 被評的是委託人（乙方評甲方） => is_client_to_freelancer = False
      - "freelancer"  -> 被評的是接案人（甲方評乙方） => is_client_to_freelancer = True
    """
    role = (role or "").strip().lower()
    if role not in ("client", "freelancer"):
        role = "client"

    is_c2f = (role == "freelancer")  # ✅ freelancer 被評 => 甲方評乙方 => True

    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""
            SELECT
              COUNT(*)::int AS count,
              ROUND(AVG((dimension_a + dimension_b + dimension_c) / 3.0)::numeric, 2) AS avg_overall,
              ROUND(AVG(dimension_a)::numeric, 2) AS avg_dim1,
              ROUND(AVG(dimension_b)::numeric, 2) AS avg_dim2,
              ROUND(AVG(dimension_c)::numeric, 2) AS avg_dim3
            FROM reviews
            WHERE reviewee_id=%s AND is_client_to_freelancer=%s
        """, (user_id, is_c2f))
        stat = cur.fetchone() or {
            "count": 0,
            "avg_overall": None,
            "avg_dim1": None,
            "avg_dim2": None,
            "avg_dim3": None,
        }

        cur.execute("""
            SELECT project_id, comment, created_at
            FROM reviews
            WHERE reviewee_id=%s AND is_client_to_freelancer=%s
              AND COALESCE(NULLIF(TRIM(comment),''), '') <> ''
            ORDER BY created_at DESC
            LIMIT %s
        """, (user_id, is_c2f, limit_comments))
        comments = cur.fetchall()

    stat["comments"] = comments
    return stat


# --- 初始化 ---
try:
    from passlib.hash import bcrypt, pbkdf2_sha256
    HAS_BCRYPT = True
except Exception:
    HAS_BCRYPT = False

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=os.getenv("SESSION_SECRET", "change-me"))
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/www", StaticFiles(directory=str(BASE_DIR / "www")), name="www")

# ----------------
# 上傳資料夾
# ----------------
UPLOAD_DIR = BASE_DIR / "www" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

BID_UPLOAD_DIR = BASE_DIR / "www" / "uploads" / "proposals"
BID_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# --------------------------------
# 首頁：專案列表
# --------------------------------
from datetime import datetime

@app.get("/", response_class=HTMLResponse)
def projects_list(request: Request):
    user = current_user(request)
    tab = request.query_params.get("tab", "open")

    q = (request.query_params.get("q") or "").strip()
    kw = f"%{q}%"

    search_sql = ""
    search_params = []
    if q:
        search_sql = """
          AND (
            p.title ILIKE %s
            OR COALESCE(p.description,'') ILIKE %s
            OR COALESCE(p.req_type,'') ILIKE %s
            OR COALESCE(p.req_deliverable,'') ILIKE %s
            OR COALESCE(p.req_deadline,'') ILIKE %s
            OR COALESCE(p.req_hours,'') ILIKE %s
            OR COALESCE(p.req_notes,'') ILIKE %s
          )
        """
        search_params = [kw] * 7

    stats = {"open": 0, "progress": 0, "closed": 0}
    projects = []

    has_new_progress = False
    has_new_closed = False

    with get_conn() as conn:
        with conn.cursor() as cur:

            # 訪客
            if not user:
                cur.execute(f"""
                    SELECT p.id, p.title, p.status, p.created_at, p.deadline,
                           LEFT(p.description, 200)
                    FROM projects p
                    WHERE p.status='open'
                    {search_sql}
                    ORDER BY p.id DESC
                """, (*search_params,))
                projects = [
                    dict(id=a, title=b, status=c, created_at=d, deadline=dl, description=e)
                    for (a,b,c,d,dl,e) in cur.fetchall()
                ]

            # 委託人
            elif user["role"] == "client":
                for k, cond in {
                    "open": "status='open'",
                    "progress": "status IN ('in_progress','reopened')",
                    "closed": "status='closed'",
                }.items():
                    cur.execute(
                        f"SELECT COUNT(*) FROM projects WHERE client_id=%s AND {cond}",
                        (user["id"],),
                    )
                    stats[k] = cur.fetchone()[0]

                if tab == "open":
                    cur.execute(f"""
                        SELECT p.id, p.title, p.status, p.created_at, p.deadline,
                               LEFT(p.description,200),
                               (SELECT COUNT(*) FROM bids b WHERE b.project_id=p.id)
                        FROM projects p
                        WHERE p.client_id=%s AND p.status='open'
                        {search_sql}
                        ORDER BY p.id DESC
                    """, (user["id"], *search_params))
                    projects = [
                        dict(id=a, title=b, status=c, created_at=d, deadline=dl,
                             description=e, bid_count=f)
                        for (a,b,c,d,dl,e,f) in cur.fetchall()
                    ]

                elif tab == "progress":
                    last_activity = _issue_last_activity_sql()

                    cur.execute(f"""
                        SELECT
                            p.id, p.title, p.status, p.created_at, p.deadline,
                            LEFT(p.description,200),
                            (SELECT COUNT(*) FROM deliveries d WHERE d.project_id=p.id) AS delivery_count,

                            (
                            CASE
                                WHEN {last_activity} > COALESCE(
                                (SELECT r.last_read_at
                                FROM project_issue_reads r
                                WHERE r.user_id=%s AND r.project_id=p.id),
                                'epoch'::timestamptz
                                )
                                THEN TRUE ELSE FALSE
                            END
                            ) AS has_new_issue

                        FROM projects p
                        WHERE p.client_id=%s AND p.status IN ('in_progress','reopened')
                        {search_sql}
                        ORDER BY p.id DESC
                    """, (user["id"], user["id"], *search_params))

                    projects = [
                        dict(
                            id=a, title=b, status=c, created_at=d, deadline=dl,
                            description=e, delivery_count=f,has_delivery=(f > 0),
                            has_new_issue=bool(g)
                        )
                        for (a,b,c,d,dl,e,f,g) in cur.fetchall()
                    ]


                else:  # closed (client)
                    with conn.cursor(row_factory=dict_row) as cur:
                        cur.execute(f"""
                            SELECT
                                p.id, p.title, p.status, p.created_at, p.deadline,
                                LEFT(p.description,200) AS description,
                                p.closed_at,
                                (
                                p.awarded_bid_id IS NOT NULL
                                AND p.closed_at IS NOT NULL
                                AND NOW() <= p.closed_at + INTERVAL '{REVIEW_WINDOW_DAYS} days'
                                AND NOT EXISTS (
                                    SELECT 1 FROM reviews r
                                    WHERE r.project_id = p.id
                                        AND r.reviewer_id = %s
                                        AND r.is_client_to_freelancer = TRUE  -- ✅ 甲方評乙方
                                )
                                ) AS needs_review
                            FROM projects p
                            WHERE p.client_id=%s AND p.status='closed'
                            {search_sql}
                            ORDER BY p.id DESC
                        """, (user["id"], user["id"], *search_params))
                        projects = cur.fetchall()




            # 接案人（紅點）
            elif user["role"] == "freelancer":

                cur.execute("SELECT COUNT(*) FROM projects WHERE status='open'")
                stats["open"] = cur.fetchone()[0]

                cur.execute("""
                    SELECT COUNT(*) FROM projects p
                    JOIN bids b ON b.id=p.awarded_bid_id
                    WHERE b.freelancer_id=%s AND p.status IN ('in_progress','reopened')
                """, (user["id"],))
                stats["progress"] = cur.fetchone()[0]

                cur.execute("""
                    SELECT COUNT(*) FROM projects p
                    JOIN bids b ON b.id=p.awarded_bid_id
                    WHERE b.freelancer_id=%s AND p.status='closed'
                """, (user["id"],))
                stats["closed"] = cur.fetchone()[0]

                with conn.cursor(row_factory=dict_row) as dcur:
                    dcur.execute("""
                        SELECT seen_progress_at, seen_closed_at
                        FROM users
                        WHERE id=%s
                    """, (user["id"],))
                    seen = dcur.fetchone() or {}
                    seen_progress_at = seen.get("seen_progress_at")
                    seen_closed_at = seen.get("seen_closed_at")

                    dcur.execute("""
                        SELECT EXISTS (
                        SELECT 1
                        FROM projects p
                        JOIN bids b ON b.id = p.awarded_bid_id
                        WHERE b.freelancer_id = %s
                            AND p.status IN ('in_progress','reopened')
                            AND p.updated_at > COALESCE(%s::timestamptz, 'epoch'::timestamptz)
                        ) AS has_new
                    """, (user["id"], seen_progress_at))
                    has_new_progress = bool(dcur.fetchone()["has_new"])

                    dcur.execute("""
                        SELECT EXISTS (
                        SELECT 1
                        FROM projects p
                        JOIN bids b ON b.id = p.awarded_bid_id
                        WHERE b.freelancer_id = %s
                            AND p.status = 'closed'
                            AND COALESCE(p.closed_at, p.updated_at) > COALESCE(%s::timestamptz, 'epoch'::timestamptz)
                        ) AS has_new
                    """, (user["id"], seen_closed_at))
                    has_new_closed = bool(dcur.fetchone()["has_new"])


                if tab == "progress":
                    cur.execute("UPDATE users SET seen_progress_at=NOW() WHERE id=%s", (user["id"],))
                    conn.commit()
                    has_new_progress = False
                elif tab == "closed":
                    cur.execute("UPDATE users SET seen_closed_at=NOW() WHERE id=%s", (user["id"],))
                    conn.commit()
                    has_new_closed = False

                if tab == "open":
                    cur.execute(f"""
                        SELECT p.id, p.title, p.status, p.created_at, p.deadline,
                               LEFT(p.description,200),
                               (SELECT COUNT(*) FROM bids b
                                WHERE b.project_id=p.id AND b.freelancer_id=%s)
                        FROM projects p
                        WHERE p.status='open'
                        {search_sql}
                        ORDER BY p.id DESC
                    """, (user["id"], *search_params))
                    projects = [
                        dict(id=a, title=b, status=c, created_at=d, deadline=dl,
                             description=e, has_bid=(f > 0))
                        for (a,b,c,d,dl,e,f) in cur.fetchall()
                    ]

                elif tab == "progress":
                    last_activity = _issue_last_activity_sql()

                    cur.execute(f"""
                        SELECT
                            p.id, p.title, p.status, p.created_at, p.deadline,
                            LEFT(p.description,200),

                            (SELECT COUNT(*) FROM deliveries d
                            WHERE d.project_id=p.id AND d.freelancer_id=%s) AS my_delivery_count,

                            (
                            CASE
                                WHEN {last_activity} > COALESCE(
                                (SELECT r.last_read_at
                                FROM project_issue_reads r
                                WHERE r.user_id=%s AND r.project_id=p.id),
                                'epoch'::timestamptz
                                )
                                THEN TRUE ELSE FALSE
                            END
                            ) AS has_new_issue

                        FROM projects p
                        JOIN bids b ON b.id=p.awarded_bid_id
                        WHERE b.freelancer_id=%s AND p.status IN ('in_progress','reopened')
                        ORDER BY p.id DESC
                    """, (user["id"], user["id"], user["id"]))

                    projects = [
                        dict(
                            id=a, title=b, status=c, created_at=d, deadline=dl,
                            description=e, my_delivery_count=f, has_delivery=(f > 0), 
                            has_new_issue=bool(g)
                        )
                        for (a,b,c,d,dl,e,f,g) in cur.fetchall()
                    ]

                else:  # closed (freelancer)
                    with conn.cursor(row_factory=dict_row) as cur:
                        cur.execute(f"""
                            SELECT
                                p.id, p.title, p.status, p.created_at, p.deadline,
                                LEFT(p.description,200) AS description,
                                p.closed_at,
                                (
                                p.closed_at IS NOT NULL
                                AND NOW() <= p.closed_at + INTERVAL '{REVIEW_WINDOW_DAYS} days'
                                AND NOT EXISTS (
                                    SELECT 1 FROM reviews r
                                    WHERE r.project_id = p.id
                                        AND r.reviewer_id = %s
                                        AND r.is_client_to_freelancer = FALSE  -- ✅ 乙方評甲方
                                )
                                ) AS needs_review
                            FROM projects p
                            JOIN bids b ON b.id = p.awarded_bid_id
                            WHERE b.freelancer_id=%s AND p.status='closed'
                            {search_sql}
                            ORDER BY p.id DESC
                        """, (user["id"], user["id"], *search_params))
                        projects = cur.fetchall()




    return templates.TemplateResponse(
        "projects_list.html",
        dict(
            request=request,
            user=user,
            tab=tab,
            q=q,
            projects=projects,
            stats=stats,
            now=datetime.now(),
            has_new_progress=has_new_progress,
            has_new_closed=has_new_closed,
        )
    )

# ----------------
# 新增專案
# ----------------
@app.get("/projects/create")
def project_create_page(request: Request):
    user = current_user(request)
    if not user or user["role"] != "client":
        return RedirectResponse("/", 302)
    return templates.TemplateResponse("project_create.html", {"request": request})

@app.post("/projects/create")
def project_create(
    request: Request,
    title: str = Form(...),
    budget: int = Form(...),
    deadline: str = Form(""),

    req_type: str = Form(""),
    req_deliverable: str = Form(""),
    req_deadline: str = Form(""),
    req_hours: str = Form(""),
    req_notes: str = Form(""),
):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", 302)
    if user["role"] != "client":
        return RedirectResponse("/", 302)

    title = (title or "").strip()
    req_type = (req_type or "").strip()
    req_deliverable = (req_deliverable or "").strip()
    req_deadline = (req_deadline or "").strip()
    req_hours = (req_hours or "").strip()
    req_notes = (req_notes or "").strip()

    deadline = (deadline or "").strip()
    if not deadline:
        return RedirectResponse("/projects/create?err=deadline", 302)

    try:
        deadline_dt = datetime.fromisoformat(deadline)
    except Exception:
        return RedirectResponse("/projects/create?err=deadline_format", 302)

    if not title or budget is None:
        return RedirectResponse("/projects/create?err=1", 302)

    has_any_detail = any([req_type, req_deliverable, req_deadline, req_hours, req_notes])
    if not has_any_detail:
        return RedirectResponse("/projects/create?err=2", 302)

    summary_parts = []
    if req_type: summary_parts.append(req_type)
    if req_deliverable: summary_parts.append(req_deliverable)
    if req_deadline: summary_parts.append(f"期限：{req_deadline}")
    description = " / ".join(summary_parts) if summary_parts else (req_notes[:80] if req_notes else "")

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO projects
            (title, description, client_id, budget, deadline,
             req_type, req_deliverable, req_deadline, req_hours, req_notes,
             updated_at)
            VALUES
            (%s, %s, %s, %s, %s,
             %s, %s, %s, %s, %s,
             NOW())
        """, (
            title, description, user["id"], budget, deadline_dt,
            req_type, req_deliverable, req_deadline, req_hours, req_notes
        ))
        conn.commit()

    return RedirectResponse("/", 302)

# ----------------
# 案子詳細資料
# ----------------
@app.get("/projects/{id}", response_class=HTMLResponse)
def project_detail(request: Request, id: int):
    user = current_user(request)

    # 讀專案（✅補上 updated_at）
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""
            SELECT
                p.id,
                p.title,
                p.description,
                p.status,
                p.created_at,
                p.updated_at,
                p.deadline,
                p.budget,

                p.req_type,
                p.req_deliverable,
                p.req_deadline,
                p.req_hours,
                p.req_notes,
                p.closed_at,

                u.username AS client_name,
                u.id       AS client_id,

                p.awarded_bid_id,
                (SELECT b.freelancer_id
                   FROM bids b
                  WHERE b.id = p.awarded_bid_id) AS awarded_freelancer_id
            FROM projects p
            JOIN users u ON p.client_id = u.id
            WHERE p.id = %s
        """, (id,))
        row = cur.fetchone()
        if not row:
            return RedirectResponse("/", 302)

        status = (row["status"] or "").strip().lower()

        project = {
            "id": row["id"],
            "title": row["title"],
            "description": row["description"],
            "status": status,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],  # ✅
            "budget": row["budget"],

            "req_type": row["req_type"],
            "req_deliverable": row["req_deliverable"],
            "req_deadline": row["req_deadline"],
            "req_hours": row["req_hours"],
            "req_notes": row["req_notes"],
            "deadline": row["deadline"],
            "client_name": row["client_name"],
            "client_id": row["client_id"],
            "awarded_bid_id": row["awarded_bid_id"],
            "awarded_freelancer_id": row["awarded_freelancer_id"],
            "closed_at": row["closed_at"],
        }

    # 進入專案詳情頁參與者 → 標記 Issue 已讀
    if user:
        uid = int(user["id"])
        participants = [int(project["client_id"])]
        if project.get("awarded_freelancer_id") is not None:
            try:
                participants.append(int(project["awarded_freelancer_id"]))
            except Exception:
                pass

        if uid in participants:
            with get_conn() as conn:
                _mark_project_issue_read(conn, uid, int(project["id"]))


    # 讀報價
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""
            SELECT 
                b.id,
                b.price,
                b.message,
                b.created_at,
                b.proposal_filename,
                b.proposal_original_name,
                fu.username AS freelancer,
                fu.id       AS freelancer_id
            FROM bids b
            JOIN users fu ON fu.id = b.freelancer_id
            WHERE b.project_id = %s
            ORDER BY b.price ASC, b.created_at ASC
        """, (id,))
        bids = cur.fetchall()

        my_bid = None
        other_bids = []
        if user and user.get("role") == "freelancer":
            uid = int(user["id"])
            for b in bids:
                if int(b["freelancer_id"]) == uid:
                    my_bid = b
                else:
                    other_bids.append(b)
        else:
            other_bids = bids

    # 讀結案檔案
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""
            SELECT
                d.id AS delivery_id,
                d.filename,
                d.note,
                d.created_at,
                u.username AS freelancer,
                d.freelancer_id
            FROM deliveries d
            JOIN users u ON u.id = d.freelancer_id
            WHERE d.project_id = %s
            ORDER BY d.created_at DESC
        """, (id,))
        deliveries = cur.fetchall()

    # ---- 評價資訊 ----
    client_review_summary = fetch_user_review_summary(project["client_id"], "client")

    freelancer_review_map = {}
    for b in bids:
        fid = int(b["freelancer_id"])
        if fid not in freelancer_review_map:
            freelancer_review_map[fid] = fetch_user_review_summary(fid, "freelancer")

    # ✅ 一開始就先定義，避免未定義
    review_open = False
    deadline_for_review = None
    my_reviewed = {"to_client": False, "to_freelancer": False}

    # ---- 是否可評價（限 closed 後 REVIEW_WINDOW_DAYS 內）----
    if project.get("status") == "closed" and project.get("closed_at"):
        closed_at = project["closed_at"]
        deadline_for_review = closed_at + timedelta(days=REVIEW_WINDOW_DAYS)
        review_open = (datetime.now() <= deadline_for_review)

        if user:
            with get_conn() as conn, conn.cursor() as cur:
                # 甲方評乙方（得標者）
                if (
                    user["role"] == "client"
                    and user["id"] == project["client_id"]
                    and project.get("awarded_freelancer_id")
                ):
                    cur.execute("""
                        SELECT 1 FROM reviews
                        WHERE project_id=%s AND reviewer_id=%s AND reviewee_id=%s
                        AND is_client_to_freelancer=TRUE
                        LIMIT 1
                    """, (project["id"], user["id"], project["awarded_freelancer_id"]))
                    my_reviewed["to_freelancer"] = bool(cur.fetchone())

                # 乙方評甲方（得標者才能評）
                if (
                    user["role"] == "freelancer"
                    and int(project.get("awarded_freelancer_id") or -1) == int(user["id"])
                ):
                    cur.execute("""
                        SELECT 1 FROM reviews
                        WHERE project_id=%s AND reviewer_id=%s AND reviewee_id=%s
                        AND is_client_to_freelancer=FALSE
                        LIMIT 1
                    """, (project["id"], user["id"], project["client_id"]))
                    my_reviewed["to_client"] = bool(cur.fetchone())


    return templates.TemplateResponse(
        "project_detail.html",
        {
            "request": request,
            "project": project,
            "user": user,
            "bids": bids,
            "my_bid": my_bid,
            "other_bids": other_bids,
            "deliveries": deliveries,

            "client_review_summary": client_review_summary,
            "freelancer_review_map": freelancer_review_map,
            "review_open": review_open,
            "review_deadline": deadline_for_review,
            "my_reviewed": my_reviewed,
            "review_window_days": REVIEW_WINDOW_DAYS,
            "open_review": (request.query_params.get("open_review") == "1"),
        }
    )

# ----------------
# 編輯專案：顯示編輯表單 / 接收編輯送出
# ----------------
import traceback

@app.get("/projects/{project_id}/edit", response_class=HTMLResponse)
def edit_project_page(request: Request, project_id: int):
    try:
        user = current_user(request)
        if not user:
            return RedirectResponse("/login", 302)

        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute("""
                SELECT id, title, description, status, client_id, budget, deadline,
                    req_type, req_deliverable, req_deadline, req_hours, req_notes
                FROM projects
                WHERE id=%s
            """, (project_id,))
            p = cur.fetchone()
            if not p:
                return RedirectResponse("/", 302)

            cur.execute(
                "SELECT EXISTS (SELECT 1 FROM bids WHERE project_id=%s) AS has_bids",
                (project_id,)
            )
            has_bids = cur.fetchone()["has_bids"]

            if user["id"] != p["client_id"] or (p["status"] or "").lower() != "open":
                return RedirectResponse(f"/projects/{project_id}", 302)

            if has_bids:
                return RedirectResponse(f"/projects/{project_id}?e=edit_locked", 302)

        deadline_val = ""
        if p.get("deadline"):
            try:
                deadline_val = p["deadline"].strftime("%Y-%m-%dT%H:%M")
            except Exception:
                deadline_val = ""

        return templates.TemplateResponse(
            "project_edit.html",
            {
                "request": request,
                "project": {
                    "id": p["id"],
                    "title": p["title"],
                    "budget": p["budget"],
                    "deadline": deadline_val,
                    "req_type": p["req_type"] or "",
                    "req_deliverable": p["req_deliverable"] or "",
                    "req_deadline": p["req_deadline"] or "",
                    "req_hours": p["req_hours"] or "",
                    "req_notes": p["req_notes"] or "",
                    "description": p["description"] or "",
                },
            },
        )

    except Exception:
        traceback.print_exc()
        return PlainTextResponse("EDIT PAGE ERROR\n\n" + traceback.format_exc(), 500)

@app.post("/projects/{project_id}/edit")
def edit_project_submit(
    request: Request,
    project_id: int,
    title: str = Form(...),
    budget: int = Form(...),
    deadline: str = Form(""),
    req_type: str = Form(""),
    req_deliverable: str = Form(""),
    req_deadline: str = Form(""),
    req_hours: str = Form(""),
    req_notes: str = Form(""),
):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", 302)

    title = (title or "").strip()
    req_type = (req_type or "").strip()
    req_deliverable = (req_deliverable or "").strip()
    req_deadline = (req_deadline or "").strip()
    req_hours = (req_hours or "").strip()
    req_notes = (req_notes or "").strip()

    deadline = (deadline or "").strip()
    if not deadline:
        return RedirectResponse(f"/projects/{project_id}/edit?err=deadline", 302)

    try:
        deadline_dt = datetime.fromisoformat(deadline)
    except Exception:
        return RedirectResponse(f"/projects/{project_id}/edit?err=deadline_format", 302)

    has_any_detail = any([req_type, req_deliverable, req_deadline, req_hours, req_notes])
    if not has_any_detail:
        return RedirectResponse(f"/projects/{project_id}/edit?err=2", 302)

    summary_parts = []
    if req_type: summary_parts.append(req_type)
    if req_deliverable: summary_parts.append(req_deliverable)
    if req_deadline: summary_parts.append(f"期限：{req_deadline}")
    description = " / ".join(summary_parts) if summary_parts else (req_notes[:80] if req_notes else "")

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT client_id, status FROM projects WHERE id=%s", (project_id,))
        row = cur.fetchone()
        if not row or row[0] != user["id"] or (row[1] or "").strip() != "open":
            return RedirectResponse(f"/projects/{project_id}", 302)

        cur.execute("SELECT EXISTS (SELECT 1 FROM bids WHERE project_id=%s)", (project_id,))
        has_bids = cur.fetchone()[0]
        if has_bids:
            return RedirectResponse(f"/projects/{project_id}?e=edit_locked", 302)

        cur.execute("""
            UPDATE projects
            SET title=%s, budget=%s, deadline=%s, description=%s,
                req_type=%s, req_deliverable=%s, req_deadline=%s, req_hours=%s, req_notes=%s,
                updated_at=NOW()
            WHERE id=%s
        """, (
            title, budget, deadline_dt, description,
            req_type, req_deliverable, req_deadline, req_hours, req_notes,
            project_id
        ))
        conn.commit()

    return RedirectResponse(f"/projects/{project_id}", 302)

# ----------------
# 刪除案子
# ----------------
@app.post("/projects/{project_id}/delete")
def delete_project(request: Request, project_id: int):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", 302)

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT client_id, status FROM projects WHERE id=%s", (project_id,))
        row = cur.fetchone()
        if not row or row[0] != user["id"] or row[1] != "open":
            return RedirectResponse(f"/projects/{project_id}", 302)

        cur.execute("SELECT EXISTS (SELECT 1 FROM bids WHERE project_id=%s)", (project_id,))
        has_bids = cur.fetchone()[0]
        if has_bids:
            return RedirectResponse(f"/projects/{project_id}?e=delete_locked", 302)

        cur.execute("DELETE FROM projects WHERE id=%s", (project_id,))
        conn.commit()

    return RedirectResponse("/", 302)

# ----------------
# 接受報價（選標）
# ----------------
@app.post("/projects/{project_id}/award/{bid_id}")
def award_bid(request: Request, project_id: int, bid_id: int):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", 302)
    if user["role"] != "client":
        return RedirectResponse(f"/projects/{project_id}", 302)

    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""
            SELECT client_id, awarded_bid_id, deadline
            FROM projects
            WHERE id=%s
        """, (project_id,))
        p = cur.fetchone()
        if not p or p["client_id"] != user["id"]:
            return RedirectResponse(f"/projects/{project_id}", 302)

        if p["awarded_bid_id"]:
            return RedirectResponse(f"/projects/{project_id}?already_awarded=1", 302)

        if p["deadline"] and datetime.now() < p["deadline"]:
            return RedirectResponse(f"/projects/{project_id}?too_early=1", 302)

        cur.execute("SELECT id FROM bids WHERE id=%s AND project_id=%s", (bid_id, project_id))
        b = cur.fetchone()
        if not b:
            return RedirectResponse(f"/projects/{project_id}?invalid_bid=1", 302)

        cur.execute("""
            UPDATE projects
            SET awarded_bid_id=%s, status='in_progress', updated_at=NOW()
            WHERE id=%s
        """, (bid_id, project_id))
        conn.commit()

    return RedirectResponse(f"/projects/{project_id}?awarded=1", 302)

# ----------------
# 上傳結案檔案
# ----------------
@app.post("/deliveries/{project_id}")
async def upload_delivery(
    request: Request,
    project_id: int,
    file: UploadFile = File(...),
    note: str = Form("")
):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", 302)
    if user["role"] != "freelancer":
        return RedirectResponse(f"/projects/{project_id}", 302)

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT p.status AS proj_status, b.freelancer_id AS awarded_freelancer_id
            FROM projects p
            JOIN bids b ON b.id = p.awarded_bid_id
            WHERE p.id = %s
        """, (project_id,))
        row = cur.fetchone()
        if not row:
            return RedirectResponse(f"/projects/{project_id}", 302)

        proj_status, awarded_freelancer_id = row

        if awarded_freelancer_id != user["id"] or proj_status not in ('in_progress', 'reopened'):
            return RedirectResponse(f"/projects/{project_id}", 302)

        cur.execute("""
            SELECT id, filename
            FROM deliveries
            WHERE project_id=%s AND freelancer_id=%s
        """, (project_id, user["id"]))
        prev = cur.fetchall()

        if prev and proj_status != 'reopened':
            return RedirectResponse(f"/projects/{project_id}?filedup=1", 302)

        if not (file.filename or "").lower().endswith(".pdf"):
            return RedirectResponse(f"/projects/{project_id}?d_pdf=0", 302)
        if file.content_type != "application/pdf":
            return RedirectResponse(f"/projects/{project_id}?d_pdf=0", 302)

    safe_original = os.path.basename(file.filename or "file.bin")
    unique_filename = f"delivery_{project_id}_{user['id']}_{uuid.uuid4().hex}_{safe_original}"
    dest = UPLOAD_DIR / unique_filename

    with open(dest, "wb") as f:
        f.write(await file.read())

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO deliveries (project_id, freelancer_id, filename, original_name, note)
            VALUES (%s,%s,%s,%s,%s)
        """, (project_id, user["id"], unique_filename, safe_original, note))
        conn.commit()

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            UPDATE projects
            SET status='in_progress', updated_at=NOW()
            WHERE id=%s AND status='reopened'
        """, (project_id,))
        conn.commit()

    return RedirectResponse(f"/projects/{project_id}", 302)

# ----------------
# 關閉案子 / 退件
# ----------------
@app.post("/projects/{project_id}/close")
def close_project(request: Request, project_id: int):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", 302)

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT client_id, status FROM projects WHERE id=%s", (project_id,))
        row = cur.fetchone()
        if not row or row[0] != user["id"] or row[1] != "in_progress":
            return RedirectResponse(f"/projects/{project_id}", 302)
        
        # ✅ 新增：檢查是否還有未完成 issue
        cur.execute("""
            SELECT COUNT(*)::int
            FROM issues
            WHERE project_id=%s AND status='open'
        """, (project_id,))
        open_cnt = cur.fetchone()[0] or 0


        if open_cnt > 0:
            return RedirectResponse(f"/projects/{project_id}?close=has_open_issues", 302)

        cur.execute("""
            UPDATE projects
            SET status='closed', closed_at=NOW(), updated_at=NOW()
            WHERE id=%s
        """, (project_id,))
        conn.commit()

    return RedirectResponse(f"/projects/{project_id}?open_review=1", 302)

@app.post("/projects/{project_id}/reject")
def reject_project(request: Request, project_id: int):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", 302)

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT client_id, status FROM projects WHERE id=%s", (project_id,))
        row = cur.fetchone()
        if not row or row[0] != user["id"] or row[1] != "in_progress":
            return RedirectResponse(f"/projects/{project_id}", 302)

        cur.execute("UPDATE projects SET status='reopened', updated_at=NOW() WHERE id=%s", (project_id,))
        conn.commit()

    return RedirectResponse(f"/projects/{project_id}?reopened=1", 302)

# ----------------
# 送出評價
# ----------------
def _clamp_star(x) -> int:
    try:
        x = int(x)
    except Exception:
        return 1
    return max(1, min(5, x))

@app.post("/projects/{project_id}/review")
def submit_review(
    request: Request,
    project_id: int,
    target: str = Form(...),      # "client" 或 "freelancer"
    dim1: int = Form(...),
    dim2: int = Form(...),
    dim3: int = Form(...),
    comment: str = Form(""),
):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", 302)

    target = (target or "").strip().lower()
    if target not in ("client", "freelancer"):
        return RedirectResponse(f"/projects/{project_id}?rv=bad_target", 302)

    # 星等檢查
    dim1 = _clamp_star(dim1)
    dim2 = _clamp_star(dim2)
    dim3 = _clamp_star(dim3)
    for v in (dim1, dim2, dim3):
        if v < 1 or v > 5:
            return RedirectResponse(f"/projects/{project_id}?rv=bad_star", 302)

    # 取案子與得標者/委託人
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""
            SELECT
              p.id, p.status, p.client_id, p.closed_at,
              (SELECT b.freelancer_id FROM bids b WHERE b.id=p.awarded_bid_id) AS awarded_freelancer_id
            FROM projects p
            WHERE p.id=%s
        """, (project_id,))
        p = cur.fetchone()
        if not p:
            return RedirectResponse("/", 302)

        if (p["status"] or "").strip().lower() != "closed":
            return RedirectResponse(f"/projects/{project_id}?rv=not_closed", 302)

        if not p["closed_at"]:
            return RedirectResponse(f"/projects/{project_id}?rv=no_closed_at", 302)

        deadline_for_review = p["closed_at"] + timedelta(days=REVIEW_WINDOW_DAYS)
        if datetime.now() > deadline_for_review:
            return RedirectResponse(f"/projects/{project_id}?rv=expired", 302)

        # 決定 reviewee + is_client_to_freelancer
        if target == "client":
            # 只有得標乙方才能評甲方
            if user["role"] != "freelancer" or int(p["awarded_freelancer_id"] or -1) != int(user["id"]):
                return RedirectResponse(f"/projects/{project_id}?rv=forbidden", 302)
            reviewee_id = int(p["client_id"])
            is_c2f = False   # ✅ 乙方評甲方

        else:  # target == "freelancer"
            # 只有甲方才能評得標乙方
            if user["role"] != "client" or int(p["client_id"]) != int(user["id"]):
                return RedirectResponse(f"/projects/{project_id}?rv=forbidden", 302)
            if not p["awarded_freelancer_id"]:
                return RedirectResponse(f"/projects/{project_id}?rv=no_award", 302)
            reviewee_id = int(p["awarded_freelancer_id"])
            is_c2f = True    # ✅ 甲方評乙方

        # 寫入（避免重複）
        try:
            cur.execute("""
                INSERT INTO reviews (
                    project_id, reviewer_id, reviewee_id,
                    is_client_to_freelancer,
                    dimension_a, dimension_b, dimension_c,
                    comment
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            """, (
                int(project_id),
                int(user["id"]),
                int(reviewee_id),
                bool(is_c2f),
                int(dim1), int(dim2), int(dim3),
                (comment or "").strip()
            ))
            conn.commit()
        except Exception:
            return RedirectResponse(f"/projects/{project_id}?rv=dup", 302)

    return RedirectResponse(f"/projects/{project_id}?rv=ok&open_review=1", 302)

# ----------------
# 送出報價
# ----------------
@app.post("/bids/{project_id}")
async def create_bid(
    request: Request,
    project_id: int,
    price: int = Form(...),
    message: str = Form(""),
    proposal_file: UploadFile = File(...),
):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", 302)
    if user["role"] != "freelancer":
        return RedirectResponse(f"/projects/{project_id}", 302)

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT deadline FROM projects WHERE id=%s", (project_id,))
        row = cur.fetchone()
        deadline = row[0] if row else None

        if deadline and datetime.now() > deadline:
            return RedirectResponse(f"/projects/{project_id}?closed=1", 302)

        cur.execute("SELECT 1 FROM bids WHERE project_id=%s AND freelancer_id=%s",
                    (project_id, user["id"]))
        if cur.fetchone():
            return RedirectResponse(f"/projects/{project_id}?dup=1", 302)

    if not (proposal_file.filename or "").lower().endswith(".pdf"):
        return RedirectResponse(f"/projects/{project_id}?pdf=0", 302)
    if proposal_file.content_type != "application/pdf":
        return RedirectResponse(f"/projects/{project_id}?pdf=0", 302)

    original_name = proposal_file.filename
    unique_name = f"proposal_{project_id}_{user['id']}_{uuid.uuid4().hex}.pdf"
    dest = BID_UPLOAD_DIR / unique_name

    with open(dest, "wb") as f:
        f.write(await proposal_file.read())

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO bids (
                project_id, freelancer_id, price, message,
                proposal_filename, proposal_original_name
            )
            VALUES (%s,%s,%s,%s,%s,%s)
        """, (
            project_id, user["id"], price, message,
            unique_name, original_name
        ))
        conn.commit()

    return RedirectResponse(f"/projects/{project_id}?bid_ok=1", 302)

# ----------------
# 登入 / 登出 / 註冊
# ----------------
@app.get("/login")
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, username, password_hash, role FROM users WHERE username=%s", (username,))
        row = cur.fetchone()
    if not row:
        return RedirectResponse("/login?e=1", 302)

    uid, uname, pw_hash, role = row
    ok = False

    if pw_hash.startswith("plain:"):
        ok = (pw_hash[6:] == password)
        if ok and HAS_BCRYPT:
            try:
                new_hash = bcrypt.hash(password)
                with get_conn() as conn, conn.cursor() as cur:
                    cur.execute("UPDATE users SET password_hash=%s WHERE id=%s", (new_hash, uid))
                    conn.commit()
            except Exception:
                pass
    else:
        if HAS_BCRYPT and (pw_hash.startswith("$2a$") or pw_hash.startswith("$2b$") or pw_hash.startswith("$2y$")):
            try:
                ok = bcrypt.verify(password, pw_hash)
            except Exception:
                ok = False

        if not ok and pw_hash.startswith("$pbkdf2-sha256$"):
            try:
                ok = pbkdf2_sha256.verify(password, pw_hash)
            except Exception:
                ok = False

    if not ok:
        return RedirectResponse("/login?e=1", 302)

    request.session["user"] = {"id": uid, "username": uname, "role": role}
    return RedirectResponse("/", 302)

@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", 302)

@app.get("/register")
def register_page(request: Request):
    e = request.query_params.get("e")
    return templates.TemplateResponse("register.html", {"request": request, "e": e})

@app.post("/register")
def register(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    password2: str = Form(...),
    role: str = Form(...),
    full_name: str = Form(...),
    phone: str = Form(""),
    email: str = Form(""),
    agree: str = Form(None),
):
    if role not in ("client", "freelancer"):
        return RedirectResponse("/register?e=role", status_code=302)
    if password != password2:
        return RedirectResponse("/register?e=pwd", status_code=302)
    if not agree:
        return RedirectResponse("/register?e=agree", status_code=302)
    if not full_name.strip():
        return RedirectResponse("/register?e=fullname", status_code=302)

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM users WHERE username=%s;", (username,))
        if cur.fetchone():
            return RedirectResponse("/register?e=user", status_code=302)

        try:
            try:
                if HAS_BCRYPT:
                    hashed = bcrypt.hash(password)
                else:
                    from passlib.hash import pbkdf2_sha256 as pbk
                    hashed = pbk.hash(password)
            except Exception:
                hashed = f"plain:{password}"

            cur.execute("""
                INSERT INTO users (username, password_hash, role, full_name, phone, email)
                VALUES (%s,%s,%s,%s,%s,%s);
            """, (username, hashed, role, full_name, (phone or None), (email or None)))
            conn.commit()

        except Exception as e:
            traceback.print_exc()
            msg = str(e).replace(" ", "_")[:120]
            return RedirectResponse(f"/register?e=dberr:{msg}", status_code=302)

    return RedirectResponse("/register?ok=1", status_code=302)

# ----------------
# 下載檔案（提案/結案）- 權限檢查 + 安全 join
# ----------------
def _safe_join(base_dir: Path, filename: str) -> Path:
    filename = os.path.basename(filename or "")
    full = (base_dir / filename).resolve()
    base = base_dir.resolve()
    if str(full).startswith(str(base)) and full.exists():
        return full
    raise HTTPException(status_code=404, detail="File not found")

@app.get("/download/proposal/{bid_id}")
def download_proposal(request: Request, bid_id: int):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", 302)

    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""
            SELECT
                b.id,
                b.freelancer_id,
                b.proposal_filename,
                b.proposal_original_name,
                p.client_id
            FROM bids b
            JOIN projects p ON p.id = b.project_id
            WHERE b.id = %s
        """, (bid_id,))
        row = cur.fetchone()
        if not row or not row["proposal_filename"]:
            raise HTTPException(status_code=404, detail="No proposal file")

        if user["id"] != row["client_id"] and user["id"] != row["freelancer_id"]:
            raise HTTPException(status_code=403, detail="Forbidden")

        file_path = _safe_join(BID_UPLOAD_DIR, row["proposal_filename"])
        download_name = row["proposal_original_name"] or row["proposal_filename"]

    return FileResponse(path=str(file_path), media_type="application/pdf", filename=download_name)

@app.get("/download/delivery/{delivery_id}")
def download_delivery(request: Request, delivery_id: int):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", 302)

    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""
            SELECT
                d.id,
                d.filename,
                d.original_name,
                d.freelancer_id,
                p.client_id,
                p.awarded_bid_id,
                (SELECT b.freelancer_id FROM bids b WHERE b.id = p.awarded_bid_id) AS awarded_freelancer_id
            FROM deliveries d
            JOIN projects p ON p.id = d.project_id
            WHERE d.id = %s
        """, (delivery_id,))
        row = cur.fetchone()
        if not row or not row["filename"]:
            raise HTTPException(status_code=404, detail="No delivery file")

        if user["id"] not in (row["client_id"], row["awarded_freelancer_id"], row["freelancer_id"]):
            raise HTTPException(status_code=403, detail="Forbidden")

        file_path = _safe_join(UPLOAD_DIR, row["filename"])
        download_name = row.get("original_name") or row["filename"]

    return FileResponse(path=str(file_path), media_type="application/pdf", filename=download_name)


# ----------------
# 查看評論
# ----------------
from fastapi.responses import JSONResponse
import math

PAGE_SIZE = 5

@app.get("/api/reviews")
def api_reviews(request: Request, target: str, user_id: int, page: int = 1):
    target = (target or "").strip().lower()
    if target not in ("client", "freelancer"):
        return JSONResponse({"error": "bad target"}, status_code=400)

    # ✅ target 決定要抓哪一種評論
    # client: 乙方評甲方 => is_client_to_freelancer = False
    # freelancer: 甲方評乙方 => is_client_to_freelancer = True
    is_c2f = (target == "freelancer")

    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        # 總數（可選：只算有留言 or 全部都算）
        cur.execute("""
            SELECT COUNT(*)::int AS total
            FROM reviews r
            WHERE r.reviewee_id = %s
              AND r.is_client_to_freelancer = %s
              AND COALESCE(NULLIF(TRIM(r.comment),''), '') <> ''
        """, (user_id, is_c2f))
        total = (cur.fetchone() or {}).get("total", 0)

        total_pages = max(1, math.ceil(total / PAGE_SIZE))

        page = max(1, int(page))
        offset = (page - 1) * PAGE_SIZE

        cur.execute("""
            SELECT
            u.username AS rater_username,
            r.comment,
            r.created_at,
            r.dimension_a,
            r.dimension_b,
            r.dimension_c
            FROM reviews r
            JOIN users u ON u.id = r.reviewer_id
            WHERE r.reviewee_id = %s
            AND r.is_client_to_freelancer = %s
            AND COALESCE(NULLIF(TRIM(r.comment),''), '') <> ''
            ORDER BY r.created_at DESC
            LIMIT %s OFFSET %s
        """, (user_id, is_c2f, PAGE_SIZE, offset))
        rows = cur.fetchall()

    items = []
    for row in rows:
        created_at = row["created_at"]
        items.append({
            "rater_username": row["rater_username"],
            "comment": row["comment"],
            "created_at": created_at.strftime("%Y-%m-%d %H:%M") if created_at else "",
            "dim1": row.get("dimension_a"),
            "dim2": row.get("dimension_b"),
            "dim3": row.get("dimension_c"),
        })


    return JSONResponse({
        "page": page,
        "total_pages": total_pages,
        "total": total,
        "items": items
    })

# ----------------
# Issue 權限檢查
# ----------------
def _get_project_participants(project_id: int):
    """
    回傳 (client_id, awarded_freelancer_id, status)
    """
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""
            SELECT
                p.client_id,
                p.status,
                (SELECT b.freelancer_id FROM bids b WHERE b.id=p.awarded_bid_id) AS awarded_freelancer_id
            FROM projects p
            WHERE p.id=%s
        """, (project_id,))
        p = cur.fetchone()
    return p  # dict or None

# ----------------
# Issue 已讀
# ----------------
def _can_access_project(user, project_row) -> bool:
    if not user or not project_row:
        return False
    uid = int(user["id"])
    return uid in (int(project_row["client_id"]), int(project_row.get("awarded_freelancer_id") or -1))


def _is_project_client(user, project_row) -> bool:
    return bool(user and project_row and user["role"] == "client" and int(user["id"]) == int(project_row["client_id"]))


def _mark_project_issue_read(conn, user_id: int, project_id: int):
    """把這個使用者在此專案的 issue 動態標記為已讀"""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO project_issue_reads (user_id, project_id, last_read_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (user_id, project_id)
            DO UPDATE SET last_read_at = EXCLUDED.last_read_at
        """, (user_id, project_id))
    conn.commit()


def _issue_last_activity_sql():
    """
    回傳一段 SQL，用來計算「某個專案最後一次 issue 活動時間」
    活動包含：
      - issues.created_at
      - issue_comments.created_at
    """
    return """
        (
          SELECT COALESCE(MAX(ts), 'epoch'::timestamptz)
          FROM (
            SELECT MAX(i.created_at) AS ts
            FROM issues i
            WHERE i.project_id = p.id

            UNION ALL

            SELECT MAX(c.created_at) AS ts
            FROM issue_comments c
            WHERE c.issue_id IN (
              SELECT id FROM issues WHERE project_id = p.id
            )
          ) t
        )
    """

# ----------------
# Issue 列表
# ----------------
@app.get("/api/issues")
def api_issues(request: Request, project_id: int):
    user = current_user(request)
    if not user:
        return JSONResponse({"error": "login required"}, status_code=401)

    p = _get_project_participants(project_id)
    if not p or not _can_access_project(user, p):
        return JSONResponse({"error": "forbidden"}, status_code=403)

    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""
            SELECT id, issue_no, project_id, delivery_id, title, description, status, created_by, created_at, resolved_at
            FROM issues
            WHERE project_id=%s
            ORDER BY
            CASE WHEN status='open' THEN 0 ELSE 1 END,
            created_at DESC
        """, (project_id,))
        items = cur.fetchall()

        # 順便帶每個 issue 的留言數（可選）
        cur.execute("""
            SELECT issue_id, COUNT(*)::int AS cnt
            FROM issue_comments
            WHERE issue_id IN (SELECT id FROM issues WHERE project_id=%s)
            GROUP BY issue_id
        """, (project_id,))
        cc = {r["issue_id"]: r["cnt"] for r in cur.fetchall()}

    for it in items:
        it["comment_count"] = cc.get(it["id"], 0)

        # ✅ 把 datetime 轉字串（不然 JSONResponse 會爆）
        if it.get("created_at"):
            it["created_at"] = it["created_at"].strftime("%Y-%m-%d %H:%M")
        if it.get("resolved_at"):
            it["resolved_at"] = it["resolved_at"].strftime("%Y-%m-%d %H:%M")

    return JSONResponse({"items": items})

# ----------------
# 甲方建立 issue
# ----------------
@app.post("/projects/{project_id}/issues/create")
def create_issue(
    request: Request,
    project_id: int,
    title: str = Form(...),
    description: str = Form(...),
    delivery_id: int = Form(None),
):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", 302)

    p = _get_project_participants(project_id)
    if not p or not _is_project_client(user, p):
        return RedirectResponse(f"/projects/{project_id}?issue=forbidden", 302)

    title = (title or "").strip()
    description = (description or "").strip()
    if not title or not description:
        return RedirectResponse(f"/projects/{project_id}?issue=empty", 302)

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            WITH ensure_counter AS (
              INSERT INTO project_issue_counters(project_id, last_no)
              VALUES (%s, 0)
              ON CONFLICT (project_id) DO NOTHING
            ),
            next_no AS (
              UPDATE project_issue_counters
              SET last_no = last_no + 1
              WHERE project_id = %s
              RETURNING last_no
            )
            INSERT INTO issues(
              project_id,
              delivery_id,
              title,
              description,
              status,
              created_by,
              issue_no
            )
            SELECT
              %s,
              %s,
              %s,
              %s,
              'open',
              %s,
              last_no
            FROM next_no
            RETURNING id, issue_no
        """, (
            project_id,        # ensure_counter
            project_id,        # next_no
            project_id,
            delivery_id,
            title,
            description,
            user["id"],
        ))

        row = cur.fetchone()
        conn.commit()

    return RedirectResponse(f"/projects/{project_id}?issue=created", 302)


# ----------------
# 甲方把 issue 設定已完成
# ----------------
from fastapi.responses import JSONResponse

@app.post("/projects/{project_id}/issues/{issue_id}/resolve")
def resolve_issue(request: Request, project_id: int, issue_id: int):
    user = current_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "login required"}, status_code=401)

    p = _get_project_participants(project_id)
    if not p or not _is_project_client(user, p):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            UPDATE issues
            SET status='resolved', resolved_at=NOW()
            WHERE id=%s AND project_id=%s AND status='open'
        """, (issue_id, project_id))
        updated = cur.rowcount
        conn.commit()

    if updated == 0:
        return JSONResponse({"ok": False, "error": "not found or already resolved"}, status_code=409)

    return JSONResponse({"ok": True})

# ----------------
# issue 留言
# ----------------
@app.post("/projects/{project_id}/issues/{issue_id}/comment")
def add_issue_comment(
    request: Request,
    project_id: int,
    issue_id: int,
    content: str = Form(...),
):
    user = current_user(request)
    if not user:
        # ✅ 前端用 fetch，比較好直接回 JSON，讓前端自己處理
        return JSONResponse({"ok": False, "error": "login required"}, status_code=401)

    p = _get_project_participants(project_id)
    if not p or not _can_access_project(user, p):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)

    content = (content or "").strip()
    if not content:
        return JSONResponse({"ok": False, "error": "comment_empty"}, status_code=400)

    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        # ✅ 確保 issue 屬於這個 project + 讀狀態
        cur.execute(
            "SELECT id, status FROM issues WHERE id=%s AND project_id=%s",
            (issue_id, project_id)
        )
        issue = cur.fetchone()
        if not issue:
            return JSONResponse({"ok": False, "error": "issue_not_found"}, status_code=404)

        # ✅ 已完成就不允許留言（雙方都一樣）
        if (issue.get("status") or "").lower() != "open":
            # ✅ 前端已經在 JS 裡處理 resp.status === 409
            return JSONResponse({"ok": False, "error": "issue_closed"}, status_code=409)

        # ✅ 寫入留言
        cur.execute("""
            INSERT INTO issue_comments (issue_id, user_id, content)
            VALUES (%s, %s, %s)
        """, (issue_id, user["id"], content))
        conn.commit()

    return JSONResponse({"ok": True})

# ----------------
# 讀取某個 issue 的留言列表
# ----------------
@app.get("/api/issue_comments")
def api_issue_comments(request: Request, project_id: int, issue_id: int):
    user = current_user(request)
    if not user:
        return JSONResponse({"error": "login required"}, status_code=401)

    p = _get_project_participants(project_id)
    if not p or not _can_access_project(user, p):
        return JSONResponse({"error": "forbidden"}, status_code=403)

    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        # 確保 issue 屬於該 project
        cur.execute("SELECT id FROM issues WHERE id=%s AND project_id=%s", (issue_id, project_id))
        if not cur.fetchone():
            return JSONResponse({"error": "not found"}, status_code=404)

        cur.execute("""
            SELECT
              c.id, c.content, c.created_at,
              u.username, u.role, u.id AS user_id
            FROM issue_comments c
            JOIN users u ON u.id = c.user_id
            WHERE c.issue_id=%s
            ORDER BY c.created_at ASC
        """, (issue_id,))
        items = cur.fetchall()

    for it in items:
        if it.get("created_at"):
            it["created_at"] = it["created_at"].strftime("%Y-%m-%d %H:%M")

    return JSONResponse({"items": items})
