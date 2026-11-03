"""Offline maintenance: caller must hold the launcher's instance lock."""
import sqlite3
from contextlib import closing
from pathlib import Path

from judging_store import Store


def restore(root, source):
    store = Store(root)
    path = Path(source).resolve()
    if path == store.path.resolve():
        raise ValueError("Выберите резервную копию, а не рабочую базу")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as source_db:
        if source_db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Резервная копия повреждена")
        tables = {r[0] for r in source_db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"settings", "event", "judges", "participants", "cards", "audit", "sessions", "snapshots"} <= tables:
            raise ValueError("Это не база сервиса судейства")
        if source_db.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone() != ("1",):
            raise ValueError("Неподдерживаемая версия резервной копии")
        backup = store.backup_internal("before_restore")
        with closing(sqlite3.connect(store.path)) as target:
            source_db.backup(target)
        with store.transaction() as db:
            db.execute("DELETE FROM sessions")
            store.log(db, "local", "restore", details={"previous_backup": backup.name})
    return backup
