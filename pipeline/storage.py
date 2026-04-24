"""Local-JSON persistence for Aims, Digests, and raw articles — swap point for Firestore in Phase 5a."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from models.schemas import Aim, AimUpdate, Digest

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
AIMS_DIR = DATA_DIR / "aims"
DIGESTS_DIR = DATA_DIR / "digests"
RAW_DIR = DATA_DIR / "raw"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(path)


def _dump(model) -> str:
    return json.dumps(model.model_dump(mode="json"), indent=2, ensure_ascii=False)


def save_aim(aim: Aim) -> None:
    _atomic_write(AIMS_DIR / f"{aim.aim_id}.json", _dump(aim))


def get_aim(aim_id: str) -> Aim | None:
    path = AIMS_DIR / f"{aim_id}.json"
    if not path.exists():
        return None
    try:
        return Aim.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, ValueError) as e:
        log.warning("get_aim: corrupted aim file %s: %s", path, e)
        return None


def update_aim(aim_id: str, patch: AimUpdate) -> Aim | None:
    current = get_aim(aim_id)
    if current is None:
        return None
    patch_dict = patch.model_dump(exclude_unset=True)
    merged = current.model_copy(update=patch_dict)
    merged.updated_at = now_iso()
    save_aim(merged)
    return merged


def delete_aim(aim_id: str) -> bool:
    path = AIMS_DIR / f"{aim_id}.json"
    existed = path.exists()
    path.unlink(missing_ok=True)
    return existed


def list_aims_for_user(user_id: str) -> list[Aim]:
    if not AIMS_DIR.exists():
        return []
    aims: list[Aim] = []
    for path in AIMS_DIR.glob("*.json"):
        try:
            aim = Aim.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, ValueError) as e:
            log.warning("list_aims_for_user: skipping corrupted %s: %s", path, e)
            continue
        if aim.user_id == user_id:
            aims.append(aim)
    aims.sort(key=lambda a: a.created_at, reverse=True)
    return aims


def save_digest(digest: Digest) -> None:
    _atomic_write(DIGESTS_DIR / f"{digest.digest_id}.json", _dump(digest))


def get_digest(digest_id: str) -> Digest | None:
    path = DIGESTS_DIR / f"{digest_id}.json"
    if not path.exists():
        return None
    try:
        return Digest.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, ValueError) as e:
        log.warning("get_digest: corrupted digest file %s: %s", path, e)
        return None


def save_raw_articles(articles: list[dict], job_id: str) -> None:
    payload = json.dumps(articles, indent=2, ensure_ascii=False)
    _atomic_write(RAW_DIR / f"{job_id}.json", payload)


def get_seen_article_ids() -> set[str]:
    if not RAW_DIR.exists():
        return set()
    seen: set[str] = set()
    for path in RAW_DIR.glob("*.json"):
        try:
            articles = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            log.warning("get_seen_article_ids: skipping corrupted %s: %s", path, e)
            continue
        if not isinstance(articles, list):
            log.warning("get_seen_article_ids: %s is not a list, skipping", path)
            continue
        for article in articles:
            if isinstance(article, dict) and "article_id" in article:
                seen.add(article["article_id"])
    return seen
