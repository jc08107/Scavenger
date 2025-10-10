"""
SQLAlchemy models for the scavenger hunt web application.

These models define the tables used to store users, invitation codes, teams,
game sessions, quests, team quests, media uploads and scores.  The schema is a
simplified version of the detailed specification.  For example, the
`invitation_codes` table does not enforce maximum uses or expiration out of
the box but includes fields for future extension.
"""

from __future__ import annotations

import datetime as _dt
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import declarative_base, relationship


Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    first_name = Column(String(100), nullable=False)
    email = Column(String(255), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(10), nullable=True)  # 'admin', 'judge', 'player'
    invitation_code_id = Column(Integer, ForeignKey("invitation_codes.id"), nullable=True)
    team_id = Column(Integer, ForeignKey("teams.id"), nullable=True)
    is_team_leader = Column(Boolean, default=False)
    created_at = Column(DateTime, default=_dt.datetime.utcnow)
    last_login_at = Column(DateTime, nullable=True)
    status = Column(String(10), default="active")

    # Relationships
    team = relationship("Team", back_populates="members", foreign_keys=[team_id])
    invitation_code = relationship("InvitationCode", back_populates="users")

    def __repr__(self) -> str:
        return f"<User id={self.id} email={self.email} role={self.role}>"


class InvitationCode(Base):
    __tablename__ = "invitation_codes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(50), unique=True, nullable=False)
    max_uses = Column(Integer, nullable=True)
    used_count = Column(Integer, default=0)
    expires_at = Column(DateTime, nullable=True)
    allowed_roles = Column(String(50), nullable=True)
    active = Column(Boolean, default=True)

    users = relationship("User", back_populates="invitation_code")

    def __repr__(self) -> str:
        return f"<InvitationCode code={self.code}>"


class Team(Base):
    __tablename__ = "teams"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), unique=True, nullable=False)
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=_dt.datetime.utcnow)

    # Relationships
    members = relationship("User", back_populates="team", foreign_keys=[User.team_id])
    team_quests = relationship("TeamQuest", back_populates="team")

    def __repr__(self) -> str:
        return f"<Team id={self.id} name={self.name}>"


class GameSession(Base):
    __tablename__ = "game_sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(200), nullable=False)
    state = Column(String(10), nullable=False, default="draft")  # 'draft', 'live', 'closed'
    launched_at = Column(DateTime, nullable=True)
    closed_at = Column(DateTime, nullable=True)

    quests = relationship("Quest", back_populates="session")

    def __repr__(self) -> str:
        return f"<GameSession id={self.id} title={self.title} state={self.state}>"


class Quest(Base):
    __tablename__ = "quests"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(Integer, ForeignKey("game_sessions.id"), nullable=False)
    quest_uid = Column(String(100), unique=True, nullable=False)
    description = Column(Text, nullable=False)
    media_required = Column(String(10), nullable=False)  # 'photo' or 'video'
    points_label = Column(String(50), nullable=False)
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=_dt.datetime.utcnow)

    session = relationship("GameSession", back_populates="quests")
    team_quests = relationship("TeamQuest", back_populates="quest")

    def __repr__(self) -> str:
        return f"<Quest id={self.id} uid={self.quest_uid}>"


class TeamQuest(Base):
    __tablename__ = "team_quests"
    __table_args__ = (UniqueConstraint("team_id", "quest_id", name="uq_team_quest"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    team_id = Column(Integer, ForeignKey("teams.id"), nullable=False)
    quest_id = Column(Integer, ForeignKey("quests.id"), nullable=False)
    latest_media_id = Column(Integer, ForeignKey("media_uploads.id"), nullable=True)

    team = relationship("Team", back_populates="team_quests")
    quest = relationship("Quest", back_populates="team_quests")
    latest_media = relationship("MediaUpload", back_populates="team_quest", foreign_keys=[latest_media_id])
    media_uploads = relationship("MediaUpload", back_populates="team_quest", foreign_keys="MediaUpload.team_quest_id")
    scores = relationship("Score", back_populates="team_quest")

    def __repr__(self) -> str:
        return f"<TeamQuest team_id={self.team_id} quest_id={self.quest_id}>"


class MediaUpload(Base):
    __tablename__ = "media_uploads"

    id = Column(Integer, primary_key=True, autoincrement=True)
    team_quest_id = Column(Integer, ForeignKey("team_quests.id"), nullable=False)
    uploaded_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    media_type = Column(String(10), nullable=False)  # 'photo' or 'video'
    file_path = Column(String(255), nullable=False)  # relative path to static/uploads directory
    size_bytes = Column(Integer, nullable=True)
    uploaded_at = Column(DateTime, default=_dt.datetime.utcnow)
    version = Column(Integer, nullable=False, default=1)

    team_quest = relationship("TeamQuest", back_populates="media_uploads", foreign_keys=[team_quest_id])
    uploaded_by = relationship("User")

    def __repr__(self) -> str:
        return f"<MediaUpload id={self.id} team_quest_id={self.team_quest_id} version={self.version}>"


class Score(Base):
    __tablename__ = "scores"
    __table_args__ = (UniqueConstraint("team_quest_id", "judge_user_id", name="uq_scores_team_judge"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    team_quest_id = Column(Integer, ForeignKey("team_quests.id"), nullable=False)
    judge_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    score = Column(Integer, nullable=False)
    scored_at = Column(DateTime, default=_dt.datetime.utcnow)
    updated_at = Column(DateTime, default=_dt.datetime.utcnow, onupdate=_dt.datetime.utcnow)

    team_quest = relationship("TeamQuest", back_populates="scores", foreign_keys=[team_quest_id])
    judge = relationship("User")

    def __repr__(self) -> str:
        return f"<Score team_quest_id={self.team_quest_id} score={self.score}>"
