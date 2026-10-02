"""Engine/session helpers. Real data: data/insight.db (or INSIGHT_DB_URL).
Sample data lives in data/insight_sample.db and never touches the real DB."""
import os

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from .dictionary import seed_metric_definitions
from .models import Base

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(REPO_ROOT, "data")
REAL_DB_PATH = os.path.join(DATA_DIR, "insight.db")
SAMPLE_DB_PATH = os.path.join(DATA_DIR, "insight_sample.db")


def sqlite_url(path):
    return "sqlite:///" + os.path.abspath(path).replace("\\", "/")


def real_db_url():
    return os.environ.get("INSIGHT_DB_URL") or sqlite_url(REAL_DB_PATH)


def make_engine(url=None):
    url = url or real_db_url()
    if url.startswith("sqlite:///") and not url.endswith(":memory:"):
        os.makedirs(os.path.dirname(url[len("sqlite:///"):]) or ".", exist_ok=True)
    engine = create_engine(url)
    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def _fk_on(dbapi_conn, _record):
            dbapi_conn.execute("PRAGMA foreign_keys=ON")
    return engine


def init_db(engine):
    """Create tables and load the metric dictionary seed. Idempotent."""
    Base.metadata.create_all(engine)
    with session_factory(engine).begin() as session:
        seed_metric_definitions(session)
    return engine


def session_factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)
