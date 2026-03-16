from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, GameSession, InvitationCode, MediaUpload, Quest, Score, Team, TeamQuest, User
from main import reset_all_teams


def make_session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    return SessionLocal()


def test_team_reset_deletes_all_non_admin_accounts_and_team_data():
    db = make_session()
    try:
        session = GameSession(title="Test Session", state="draft")
        admin_code = InvitationCode(code="ADMIN", max_uses=2, used_count=1, active=True)
        player_code = InvitationCode(code="PLAYER", max_uses=1, used_count=1, active=False)
        admin = User(
            first_name="Admin",
            email="admin@example.com",
            password_hash="hash",
            role="admin",
            invitation_code=admin_code,
        )
        player = User(
            first_name="Player",
            email="player@example.com",
            password_hash="hash",
            role="player",
            invitation_code=player_code,
            is_team_leader=True,
        )
        judge = User(
            first_name="Judge",
            email="judge@example.com",
            password_hash="hash",
            role="judge",
            invitation_code=player_code,
        )
        pending = User(
            first_name="Pending",
            email="pending@example.com",
            password_hash="hash",
            invitation_code=player_code,
        )
        team = Team(name="Red", created_by_user_id=player.id)
        quest = Quest(
            session=session,
            quest_uid="Q1",
            description="Quest",
            media_required="photo",
            points_label="10",
        )
        db.add_all([session, admin_code, player_code, admin, player, judge, pending, team, quest])
        db.flush()

        player.team_id = team.id
        team_quest = TeamQuest(team_id=team.id, quest_id=quest.id)
        db.add(team_quest)
        db.flush()

        media = MediaUpload(
            team_quest_id=team_quest.id,
            uploaded_by_user_id=player.id,
            media_type="photo",
            file_path="uploads/test.jpg",
            version=1,
        )
        db.add(media)
        db.flush()

        score = Score(team_quest_id=team_quest.id, judge_user_id=judge.id, score=5)
        db.add(score)
        db.commit()

        reset_all_teams(db, admin_user_id=admin.id)
        db.commit()

        remaining_users = db.query(User).all()
        assert [user.email for user in remaining_users] == ["admin@example.com"]
        assert db.query(Team).count() == 0
        assert db.query(TeamQuest).count() == 0
        assert db.query(MediaUpload).count() == 0
        assert db.query(Score).count() == 0

        db.refresh(admin_code)
        db.refresh(player_code)
        assert admin_code.used_count == 1
        assert admin_code.active is True
        assert player_code.used_count == 0
        assert player_code.active is True
    finally:
        db.close()
