import sqlite3
import os
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "agent_database.db")

load_dotenv(os.path.join(BASE_DIR, ".env"))

# SQLite key -> .env variable name, for keys whose names differ between the two stores.
ENV_NAMES = {
    "META_GRAPH_API_KEY": "META_ACCESS_TOKEN",
    "IG_ACCOUNT_ID": "IG_USER_ID",
}
# Lets callers use either name (e.g. get_setting("META_ACCESS_TOKEN")).
DB_NAMES = {env: db_key for db_key, env in ENV_NAMES.items()}

# Secrets: .env wins over SQLite. Every other key: SQLite wins, .env is the fallback.
ENV_FIRST = {"META_GRAPH_API_KEY", "IG_ACCOUNT_ID", "GEMINI_API_KEY"}

_reported_sources = set()

def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')
    conn.commit()
    conn.close()

def save_setting(key, value):
    key = DB_NAMES.get(key, key)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO settings (key, value) 
        VALUES (?, ?) 
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
    ''', (key, value))
    conn.commit()
    conn.close()

def get_stored_setting(key):
    """SQLite value only, ignoring .env."""
    key = DB_NAMES.get(key, key)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT value FROM settings WHERE key = ?', (key,))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result and result[0] else ""

def _resolve(key):
    """Returns (value, source) where source is '.env', 'sqlite' or 'unset'."""
    key = DB_NAMES.get(key, key)
    env_value = os.environ.get(ENV_NAMES.get(key, key), "")
    db_value = get_stored_setting(key)

    if key in ENV_FIRST:
        candidates = [(env_value, ".env"), (db_value, "sqlite")]
    else:
        candidates = [(db_value, "sqlite"), (env_value, ".env")]

    for value, source in candidates:
        if value:
            return value, source
    return "", "unset"

def setting_source(key):
    return _resolve(key)[1]

def get_setting(key):
    value, source = _resolve(key)
    # Log where each key came from (never the value), once per key/source per process.
    if (key, source) not in _reported_sources:
        _reported_sources.add((key, source))
        print(f"[settings] {key} <- {source}")
    return value

init_db()

if __name__ == "__main__":
    for k in ["META_GRAPH_API_KEY", "IG_ACCOUNT_ID", "GEMINI_API_KEY", "GRAPH_HOST", "GRAPH_VERSION", "PUBLISH_MODE"]:
        get_setting(k)
