"""
Main entry point for the scavenger hunt web application.

This module defines the FastAPI application, configures middleware (sessions and
static files), initialises the database, and defines routes for user
authentication, team management, quest interactions, judging and admin
operations.  The application uses Jinja2 templates for HTML rendering and
serves static assets from the `static/` directory.

For simplicity this implementation assumes a single game session titled
"Scavenger Hunt 2025".  If no session exists at startup, one will be created.
"""

from __future__ import annotations

import csv
import os
import uuid
from datetime import datetime
from typing import List, Optional

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.orm import Session, subqueryload

from starlette.middleware.sessions import SessionMiddleware

from app import database
from app.auth import get_current_user, get_password_hash, require_role, verify_password
from app.models import (
    Base,
    GameSession,
    InvitationCode,
    MediaUpload,
    Quest,
    Score,
    Team,
    TeamQuest,
    User,
)



# Instantiate the FastAPI application
app = FastAPI(title="Scavenger Hunt 2025")


# Configure session middleware.  This stores session data in a signed cookie.
SECRET_KEY = os.getenv("SCAVENGER_SECRET_KEY", str(uuid.uuid4()))
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)


# Mount the static directory to serve uploaded media and CSS.  The static
# directory is relative to this module's parent folder.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

if not os.path.exists(os.path.join(STATIC_DIR, "uploads")):
    os.makedirs(os.path.join(STATIC_DIR, "uploads"), exist_ok=True)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# Configure Jinja2 templates
TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

MAX_JUDGES = 2


def ensure_scores_unique_index() -> None:
    """Ensure the scores table enforces uniqueness per judge/team quest pair."""
    engine = database.engine
    if engine.dialect.name != "sqlite":
        return
    with engine.begin() as conn:
        index_rows = conn.execute(text("PRAGMA index_list('scores')")).fetchall()
        legacy_unique_indexes: List[str] = []
        composite_exists = False
        for idx in index_rows:
            idx_name = idx[1]
            is_unique = idx[2] == 1
            if not is_unique or not idx_name:
                continue
            columns = conn.execute(text(f"PRAGMA index_info('{idx_name}')")).fetchall()
            col_names = [col[2] for col in columns]
            if col_names == ["team_quest_id"]:
                legacy_unique_indexes.append(idx_name)
            if col_names == ["team_quest_id", "judge_user_id"]:
                composite_exists = True
        needs_rebuild = any(name.startswith("sqlite_autoindex") for name in legacy_unique_indexes)
        if needs_rebuild:
            conn.execute(text("PRAGMA foreign_keys=OFF"))
            try:
                conn.execute(text("ALTER TABLE scores RENAME TO scores_old"))
                conn.execute(
                    text(
                        """
                        CREATE TABLE scores (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            team_quest_id INTEGER NOT NULL,
                            judge_user_id INTEGER NOT NULL,
                            score INTEGER NOT NULL,
                            scored_at DATETIME,
                            updated_at DATETIME,
                            FOREIGN KEY(team_quest_id) REFERENCES team_quests(id),
                            FOREIGN KEY(judge_user_id) REFERENCES users(id),
                            UNIQUE(team_quest_id, judge_user_id)
                        )
                        """
                    )
                )
                conn.execute(
                    text(
                        """
                        INSERT INTO scores (id, team_quest_id, judge_user_id, score, scored_at, updated_at)
                        SELECT id, team_quest_id, judge_user_id, score, scored_at, updated_at
                        FROM scores_old
                        """
                    )
                )
                conn.execute(text("DROP TABLE scores_old"))
            finally:
                conn.execute(text("PRAGMA foreign_keys=ON"))
        else:
            for idx_name in legacy_unique_indexes:
                if idx_name.startswith("sqlite_autoindex"):
                    continue  # auto indexes can only be cleared via rebuild
                conn.execute(text(f'DROP INDEX IF EXISTS "{idx_name}"'))
            if not composite_exists:
                conn.execute(
                    text(
                        "CREATE UNIQUE INDEX IF NOT EXISTS uq_scores_team_judge "
                        "ON scores(team_quest_id, judge_user_id)"
                    )
                )


@app.on_event("startup")
def on_startup() -> None:
    """Initialise the database and create a default game session if necessary."""
    # Create all tables
    Base.metadata.create_all(bind=database.engine)
    # Create a default invitation code if none exists
    db = database.SessionLocal()
    try:
        # Ensure at least one session exists
        session = db.query(GameSession).first()
        if session is None:
            session = GameSession(title="Scavenger Hunt 2025", state="draft")
            db.add(session)
            db.commit()
        # Ensure a default invitation code exists for players
        code = db.query(InvitationCode).filter(InvitationCode.code == "DEFAULT").first()
        if code is None:
            code = InvitationCode(code="DEFAULT", allowed_roles="player,judge", active=True)
            db.add(code)
            db.commit()
    finally:
        db.close()
    ensure_scores_unique_index()


def get_session(db: Session) -> GameSession:
    """Retrieve the current game session (assumes single session)."""
    session = db.query(GameSession).first()
    if not session:
        raise HTTPException(500, detail="Game session not initialized")
    return session


# ----------------------- Helper Functions -----------------------

def ensure_team_quests(db: Session, team: Team, session: GameSession) -> None:
    """Ensure that a TeamQuest exists for every Quest in the given session.

    If the team is new or new quests were added after team creation, this helper
    creates the missing TeamQuest records.  It does not update existing
    associations.
    """
    quest_ids = {q.id for q in session.quests if q.active}
    existing_pairs = {
        (tq.team_id, tq.quest_id)
        for tq in db.query(TeamQuest).filter(TeamQuest.team_id == team.id).all()
    }
    for qid in quest_ids:
        if (team.id, qid) not in existing_pairs:
            tq = TeamQuest(team_id=team.id, quest_id=qid)
            db.add(tq)
    db.commit()


# ----------------------- Route Handlers -----------------------

@app.get("/", response_class=HTMLResponse)
def root(request: Request) -> HTMLResponse:
    """Home page redirect to login if not authenticated."""
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse(url="/login", status_code=302)

    db = database.SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
    finally:
        db.close()

    if not user:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=302)

    if user.role == "player":
        if user.team_id:
            return RedirectResponse(url="/player/home", status_code=302)
        return RedirectResponse(url="/player/teams", status_code=302)
    if user.role == "judge":
        return RedirectResponse(url="/judge/teams", status_code=302)
    if user.role == "admin":
        return RedirectResponse(url="/admin/dashboard", status_code=302)
    return RedirectResponse(url="/role", status_code=302)


@app.get("/signup", response_class=HTMLResponse)
def signup_get(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("signup.html", {"request": request, "error": None})


@app.post("/signup", response_class=HTMLResponse)
def signup_post(
    request: Request,
    first_name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    invitation_code: str = Form(...),
    db: Session = Depends(database.get_db),
) -> HTMLResponse:
    # Check invitation code
    code = db.query(InvitationCode).filter(InvitationCode.code == invitation_code).first()
    if code is None or not code.active:
        return templates.TemplateResponse(
            "signup.html", {"request": request, "error": "Invalid invitation code"}
        )
    # Check email uniqueness
    if db.query(User).filter(User.email == email).first():
        return templates.TemplateResponse(
            "signup.html", {"request": request, "error": "Email already registered"}
        )
    # Create user with hashed password
    user = User(
        first_name=first_name,
        email=email,
        password_hash=get_password_hash(password),
        role=None,
        invitation_code_id=code.id,
    )
    db.add(user)
    # Increment used_count if applicable
    if code.max_uses is not None:
        code.used_count += 1
        if code.used_count >= code.max_uses:
            code.active = False
    db.commit()
    # Redirect to login
    return RedirectResponse(url="/login", status_code=302)


@app.get("/login", response_class=HTMLResponse)
def login_get(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.post("/login", response_class=HTMLResponse)
def login_post(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(database.get_db),
) -> HTMLResponse:
    user = db.query(User).filter(User.email == email).first()
    if not user or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            "login.html", {"request": request, "error": "Invalid email or password"}
        )
    # Save user_id to session
    request.session["user_id"] = user.id
    # Update last login timestamp
    user.last_login_at = datetime.utcnow()
    db.commit()
    return RedirectResponse(url="/", status_code=302)


@app.get("/logout")
def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse(url="/login", status_code=302)


@app.get("/role", response_class=HTMLResponse)
def role_get(
    request: Request,
    db: Session = Depends(database.get_db),
    user: User = Depends(get_current_user),
) -> HTMLResponse:
    if user.role:
        # If already selected role, redirect
        if user.role == "player":
            target = "/player/home" if user.team_id else "/player/teams"
            return RedirectResponse(url=target, status_code=302)
        elif user.role == "judge":
            return RedirectResponse(url="/judge/teams", status_code=302)
        elif user.role == "admin":
            return RedirectResponse(url="/admin/dashboard", status_code=302)
    admin_taken = db.query(User).filter(User.role == "admin").first() is not None
    judge_count = db.query(User).filter(User.role == "judge").count()
    judge_slots_available = max(MAX_JUDGES - judge_count, 0)
    judge_available = judge_slots_available > 0 or user.role == "judge"
    if admin_taken and judge_slots_available == 0 and not user.role:
        user.role = "player"
        db.commit()
        target = "/player/home" if user.team_id else "/player/teams"
        return RedirectResponse(url=target, status_code=302)
    return templates.TemplateResponse(
        "role_select.html",
        {
            "request": request,
            "admin_available": not admin_taken,
            "judge_available": judge_available,
            "judge_slots_available": judge_slots_available,
        },
    )


@app.post("/role")
def role_post(request: Request, role: str = Form(...), db: Session = Depends(database.get_db), user: User = Depends(get_current_user)) -> RedirectResponse:
    # Only allow valid roles
    if role not in {"player", "judge", "admin"}:
        raise HTTPException(400, detail="Invalid role selected")
    if role == "admin":
        existing_admin = db.query(User).filter(User.role == "admin").first()
        if existing_admin and existing_admin.id != user.id:
            raise HTTPException(400, detail="Admin role already assigned")
    if role == "judge":
        other_judges = (
            db.query(User)
            .filter(User.role == "judge", User.id != user.id)
            .count()
        )
        if other_judges >= MAX_JUDGES:
            raise HTTPException(400, detail="Judge roles are already filled")
    user.role = role
    db.commit()
    # Redirect accordingly
    if role == "player":
        next_url = "/player/home" if user.team_id else "/player/teams"
        return RedirectResponse(url=next_url, status_code=302)
    elif role == "judge":
        return RedirectResponse(url="/judge/teams", status_code=302)
    else:
        return RedirectResponse(url="/admin/dashboard", status_code=302)


# ----------------------- Player Routes -----------------------

@app.get("/player/teams", response_class=HTMLResponse)
def player_teams_get(request: Request, db: Session = Depends(database.get_db), user: User = Depends(require_role("player"))) -> HTMLResponse:
    if user.team_id:
        return RedirectResponse(url="/player/home", status_code=302)
    # Retrieve existing teams
    teams = db.query(Team).all()
    return templates.TemplateResponse(
        "player_teams.html",
        {
            "request": request,
            "teams": teams,
        },
    )


@app.post("/player/teams")
def player_teams_post(
    request: Request,
    team_id: Optional[str] = Form(None),
    new_team_name: Optional[str] = Form(None),
    db: Session = Depends(database.get_db),
    user: User = Depends(require_role("player")),
) -> RedirectResponse:
    if user.team_id:
        return RedirectResponse(url="/player/home", status_code=302)
    session = get_session(db)

    # Normalize inputs
    team_id = (team_id or "").strip()
    new_team_name = (new_team_name or "").strip()

    # Create new team if provided
    if new_team_name:
        # Basic validation
        if len(new_team_name) < 3:
            raise HTTPException(400, detail="Team name must be at least 3 characters")
        # Check unique name
        if db.query(Team).filter(Team.name == new_team_name).first():
            # Name taken; you could re-render with a message; for now just 400:
            raise HTTPException(400, detail="Team name already in use")
        team = Team(name=new_team_name, created_by_user_id=user.id)
        db.add(team)
        db.commit()
        user.team_id = team.id
        user.is_team_leader = True
        db.commit()
        ensure_team_quests(db, team, session)
        return RedirectResponse(url="/player/home", status_code=302)

    # Join existing team if a non-empty team_id was submitted
    if team_id:
        try:
            tid = int(team_id)
        except ValueError:
            raise HTTPException(400, detail="Invalid team ID")
        team = db.query(Team).filter(Team.id == tid).first()
        if not team:
            raise HTTPException(404, detail="Team not found")
        user.team_id = team.id
        user.is_team_leader = False
        db.commit()
        ensure_team_quests(db, team, session)
        return RedirectResponse(url="/player/home", status_code=302)

    # Neither a new name nor a valid team_id was provided
    raise HTTPException(400, detail="Please choose a team or enter a new team name")


@app.get("/player/home", response_class=HTMLResponse)
def player_home(
    request: Request,
    db: Session = Depends(database.get_db),
    user: User = Depends(require_role("player")),
) -> HTMLResponse:
    if not user.team_id:
        return RedirectResponse(url="/player/teams", status_code=302)

    team = db.query(Team).filter(Team.id == user.team_id).first()
    if not team:
        user.team_id = None
        user.is_team_leader = False
        db.commit()
        return RedirectResponse(url="/player/teams", status_code=302)

    roster = (
        db.query(User)
        .filter(User.team_id == team.id)
        .order_by(User.first_name.asc(), User.id.asc())
        .all()
    )
    return templates.TemplateResponse(
        "player_home.html",
        {
            "request": request,
            "team": team,
            "roster": roster,
        },
    )


@app.get("/player/quests", response_class=HTMLResponse)
def player_quests_get(request: Request, db: Session = Depends(database.get_db), user: User = Depends(require_role("player"))) -> HTMLResponse:
    if not user.team_id:
        return RedirectResponse(url="/player/teams", status_code=302)
    session = get_session(db)
    team = db.query(Team).filter(Team.id == user.team_id).first()
    ensure_team_quests(db, team, session)
    # Fetch quests with status
    team_quests = (
        db.query(TeamQuest)
        .filter(TeamQuest.team_id == team.id)
        .join(Quest)
        .filter(Quest.session_id == session.id)
        .order_by(Quest.id)
        .all()
    )
    quests_data = []
    for tq in team_quests:
        media = tq.latest_media
        status = "pending"
        if media:
            status = "uploaded"
        quests_data.append({
            "team_quest_id": tq.id,
            "quest_uid": tq.quest.quest_uid,
            "description": tq.quest.description,
            "media_required": tq.quest.media_required,
            "points_label": tq.quest.points_label,
            "status": status,
            "upload_btn_label": "Upload Again" if media else "Upload",
            "media_file": media.file_path if media else None,
        })
    return templates.TemplateResponse(
        "player_quests.html",
        {
            "request": request,
            "team_name": team.name,
            "quests": quests_data,
            "game_state": session.state,
        },
    )


@app.post("/player/upload/{team_quest_id}")
def player_upload(
    request: Request,
    team_quest_id: int,
    upload_file: UploadFile = File(...),
    db: Session = Depends(database.get_db),
    user: User = Depends(require_role("player")),
) -> RedirectResponse:
    # Ensure the user belongs to the team quest
    tq = db.query(TeamQuest).filter(TeamQuest.id == team_quest_id).first()
    if not tq or tq.team_id != user.team_id:
        raise HTTPException(403, detail="Forbidden")
    quest = tq.quest
    # Validate media type
    if quest.media_required == "photo" and not upload_file.content_type.startswith("image/"):
        raise HTTPException(400, detail="Invalid media type; expected photo")
    if quest.media_required == "video" and not upload_file.content_type.startswith("video/"):
        raise HTTPException(400, detail="Invalid media type; expected video")
    # Save file to uploads directory with unique name
    file_ext = os.path.splitext(upload_file.filename)[1]
    unique_name = f"{uuid.uuid4()}{file_ext}"
    save_path = os.path.join(STATIC_DIR, "uploads", unique_name)
    with open(save_path, "wb") as f:
        f.write(upload_file.file.read())
    file_size = os.path.getsize(save_path)
    # Determine version
    current_version = 1
    if tq.latest_media:
        current_version = tq.latest_media.version + 1
    # Create media upload record
    media = MediaUpload(
        team_quest_id=tq.id,
        uploaded_by_user_id=user.id,
        media_type=quest.media_required,
        file_path=f"uploads/{unique_name}",
        size_bytes=file_size,
        version=current_version,
    )
    db.add(media)
    db.commit()
    # Update team quest latest_media_id
    tq.latest_media_id = media.id
    db.commit()
    return RedirectResponse(url="/player/quests", status_code=302)


# ----------------------- Judge Routes -----------------------

@app.get("/judge/teams", response_class=HTMLResponse)
def judge_teams_get(request: Request, db: Session = Depends(database.get_db), user: User = Depends(require_role("judge"))) -> HTMLResponse:
    # List teams that have quests in the session
    session = get_session(db)
    teams = db.query(Team).all()
    return templates.TemplateResponse("judge_teams.html", {"request": request, "teams": teams})


@app.get("/judge/ballot/{team_id}", response_class=HTMLResponse)
def judge_ballot_get(
    request: Request,
    team_id: int,
    db: Session = Depends(database.get_db),
    user: User = Depends(require_role("judge")),
) -> HTMLResponse:
    team = db.query(Team).filter(Team.id == team_id).first()
    if not team:
        raise HTTPException(404, detail="Team not found")
    session = get_session(db)
    # Ensure team quests exist
    ensure_team_quests(db, team, session)
    # Fetch quests and associated media and score
    team_quests = (
        db.query(TeamQuest)
        .filter(TeamQuest.team_id == team.id)
        .join(Quest)
        .filter(Quest.session_id == session.id)
        .order_by(Quest.id)
        .all()
    )
    team_quest_ids = [tq.id for tq in team_quests]
    scores_for_user = {}
    if team_quest_ids:
        user_scores = (
            db.query(Score)
            .filter(Score.team_quest_id.in_(team_quest_ids), Score.judge_user_id == user.id)
            .all()
        )
        scores_for_user = {score.team_quest_id: score for score in user_scores}
    quests_data = []
    for tq in team_quests:
        media = tq.latest_media
        status = "pending"
        media_url = None
        if media:
            status = "uploaded"
            media_url = f"/static/{media.file_path}"
        score_record = scores_for_user.get(tq.id)
        score_value = score_record.score if score_record else None
        quests_data.append({
            "team_quest_id": tq.id,
            "quest_uid": tq.quest.quest_uid,
            "description": tq.quest.description,
            "media_required": tq.quest.media_required,
            "points_label": tq.quest.points_label,
            "status": status,
            "media_url": media_url,
            "score": score_value,
        })
    return templates.TemplateResponse(
        "judge_ballot.html",
        {
            "request": request,
            "team": team,
            "quests": quests_data,
        },
    )


@app.post("/judge/score")
def judge_score_post(
    request: Request,
    team_quest_id: int = Form(...),
    score: int = Form(...),
    db: Session = Depends(database.get_db),
    user: User = Depends(require_role("judge")),
) -> RedirectResponse:
    # Validate score non-negative
    if score < 0:
        raise HTTPException(400, detail="Score must be zero or positive")
    tq = db.query(TeamQuest).filter(TeamQuest.id == team_quest_id).first()
    if not tq:
        raise HTTPException(404, detail="TeamQuest not found")
    # Upsert score
    score_record = (
        db.query(Score)
        .filter(Score.team_quest_id == tq.id, Score.judge_user_id == user.id)
        .first()
    )
    if not score_record:
        score_record = Score(team_quest_id=tq.id, judge_user_id=user.id, score=score)
        db.add(score_record)
    else:
        score_record.score = score
        score_record.updated_at = datetime.utcnow()
    db.commit()
    # Redirect back to ballot page
    return RedirectResponse(url=f"/judge/ballot/{tq.team_id}", status_code=302)


@app.get("/judge/results/{team_id}", response_class=HTMLResponse)
def judge_team_results(
    request: Request,
    team_id: int,
    db: Session = Depends(database.get_db),
    user: User = Depends(require_role("judge")),
) -> HTMLResponse:
    team = db.query(Team).filter(Team.id == team_id).first()
    if not team:
        raise HTTPException(404, detail="Team not found")
    session = get_session(db)
    # Ensure team quests exist
    ensure_team_quests(db, team, session)
    team_quests = (
        db.query(TeamQuest)
        .filter(TeamQuest.team_id == team.id)
        .join(Quest)
        .filter(Quest.session_id == session.id)
        .options(
            subqueryload(TeamQuest.scores),
            subqueryload(TeamQuest.quest),
            subqueryload(TeamQuest.latest_media),
        )
        .order_by(Quest.id)
        .all()
    )
    outstanding = []
    quest_rows = []
    for tq in team_quests:
        judge_score = None
        all_scores = []
        for score_entry in tq.scores:
            all_scores.append(score_entry.score)
            if score_entry.judge_user_id == user.id:
                judge_score = score_entry.score
        if tq.latest_media and judge_score is None:
            outstanding.append(tq)
        average_score = None
        if all_scores:
            average_score = round(sum(all_scores) / len(all_scores), 2)
        quest_rows.append(
            {
                "quest_uid": tq.quest.quest_uid,
                "description": tq.quest.description,
                "points_label": tq.quest.points_label,
                "your_score": judge_score,
                "average_score": average_score,
            }
        )
    if outstanding:
        out_info = [tq.quest.quest_uid for tq in outstanding]
        return templates.TemplateResponse(
            "judge_outstanding.html",
            {
                "request": request,
                "team": team,
                "outstanding": out_info,
            },
        )
    your_scores = [row["your_score"] for row in quest_rows if row["your_score"] is not None]
    your_total = sum(your_scores)
    average_values = [row["average_score"] for row in quest_rows if row["average_score"] is not None]
    average_total = round(sum(average_values), 2) if average_values else None
    judge_ids = {
        score_entry.judge_user_id
        for tq in team_quests
        for score_entry in tq.scores
    }
    judge_count = len(judge_ids)
    return templates.TemplateResponse(
        "judge_team_results.html",
        {
            "request": request,
            "team": team,
            "quests": quest_rows,
            "your_total": your_total,
            "average_total": average_total,
            "judge_count": judge_count,
        },
    )


# ----------------------- Admin Routes -----------------------

@app.get("/admin/dashboard", response_class=HTMLResponse)
def admin_dashboard(
    request: Request, db: Session = Depends(database.get_db), user: User = Depends(require_role("admin"))
) -> HTMLResponse:
    session = get_session(db)
    teams = (
        db.query(Team)
        .options(subqueryload(Team.members))
        .order_by(Team.name.asc())
        .all()
    )
    team_groups = []
    for team in teams:
        members = sorted(team.members, key=lambda member: member.first_name.lower())
        team_groups.append({"team": team, "members": members})
    awaiting_members = (
        db.query(User)
        .filter(User.team_id.is_(None))
        .order_by(User.first_name.asc())
        .all()
    )
    return templates.TemplateResponse(
        "admin_dashboard.html",
        {
            "request": request,
            "game_state": session.state,
            "team_groups": team_groups,
            "awaiting_members": awaiting_members,
        },
    )


@app.post("/admin/state")
def admin_change_state(
    request: Request,
    new_state: str = Form(...),
    db: Session = Depends(database.get_db),
    user: User = Depends(require_role("admin")),
) -> RedirectResponse:
    session = get_session(db)
    if new_state not in {"draft", "live", "closed"}:
        raise HTTPException(400, detail="Invalid state")
    session.state = new_state
    if new_state == "live":
        session.launched_at = datetime.utcnow()
    if new_state == "closed":
        session.closed_at = datetime.utcnow()
    db.commit()
    return RedirectResponse(url="/admin/dashboard", status_code=302)


@app.get("/admin/upload-quests", response_class=HTMLResponse)
def admin_upload_get(
    request: Request, user: User = Depends(require_role("admin"))
) -> HTMLResponse:
    return templates.TemplateResponse("admin_upload.html", {"request": request})


@app.post("/admin/upload-quests")
def admin_upload_post(
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(database.get_db),
    user: User = Depends(require_role("admin")),
) -> RedirectResponse:
    # Parse CSV file; expected columns: quest_uid, description, media_required, points_label
    raw_bytes = file.file.read()
    try:
        contents = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        contents = raw_bytes.decode("cp1252")
    reader = csv.DictReader(contents.splitlines())
    session = get_session(db)
    for row in reader:
        uid = row.get("quest_uid")
        desc = row.get("description")
        media_required = row.get("media_required")
        points_label = row.get("points_label")
        if not uid or not desc or not media_required:
            continue  # skip incomplete rows
        # Check existing quest
        quest = db.query(Quest).filter(Quest.quest_uid == uid).first()
        if quest:
            quest.description = desc
            quest.media_required = media_required
            quest.points_label = points_label or ""
        else:
            quest = Quest(
                session_id=session.id,
                quest_uid=uid,
                description=desc,
                media_required=media_required,
                points_label=points_label or "",
            )
            db.add(quest)
    db.commit()
    return RedirectResponse(url="/admin/dashboard", status_code=302)
