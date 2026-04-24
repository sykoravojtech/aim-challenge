"""Persistence for Aims, Digests, and raw articles.

Local JSON is the default and remains the source of truth during the Phase 5a
transition. When ``USE_FIRESTORE`` is truthy (``1``/``true``/``yes``, case-
insensitive), writes are mirrored to Firestore and reads try Firestore first,
falling back to local JSON on miss or error. Raw articles and the dedup seen-
set stay JSON-only (BigQuery mirror lives in ingestion; GCS is Phase 5e).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from models.schemas import Aim, AimUpdate, Digest

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
AIMS_DIR = DATA_DIR / "aims"
DIGESTS_DIR = DATA_DIR / "digests"
RAW_DIR = DATA_DIR / "raw"

# ---------------------------------------------------------------------------
# Firestore toggle + lazy client
# ---------------------------------------------------------------------------

_USE_FIRESTORE = os.getenv("USE_FIRESTORE", "").strip().lower() in {"1", "true", "yes"}

# Sentinel: None == not yet tried; False == tried and failed; else client instance.
_FS_UNSET: Any = object()
_fs_client: Any = _FS_UNSET


def _get_fs_client():
    """Return a cached Firestore client, or None if disabled/unavailable.

    Initialised lazily so importing this module never touches google-cloud-firestore
    unless USE_FIRESTORE is truthy.
    """
    global _fs_client
    if not _USE_FIRESTORE:
        return None
    if _fs_client is _FS_UNSET:
        try:
            from google.cloud import firestore  # type: ignore

            _fs_client = firestore.Client()
            log.info("[firestore] client initialised (default database)")
        except Exception as e:  # noqa: BLE001 — any init failure should degrade, not crash
            log.warning("[firestore] client init failed, falling back to local JSON: %s", e)
            _fs_client = None
    return _fs_client


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(path)


def _dump(model) -> str:
    return json.dumps(model.model_dump(mode="json"), indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Aims
# ---------------------------------------------------------------------------


def save_aim(aim: Aim) -> None:
    _atomic_write(AIMS_DIR / f"{aim.aim_id}.json", _dump(aim))
    client = _get_fs_client()
    if client is not None:
        try:
            client.collection("aims").document(aim.aim_id).set(aim.model_dump(mode="json"))
            log.info("[firestore] wrote aims/%s", aim.aim_id)
        except Exception as e:  # noqa: BLE001
            log.warning("[firestore] save_aim %s failed: %s", aim.aim_id, e)


def _get_aim_local(aim_id: str) -> Aim | None:
    path = AIMS_DIR / f"{aim_id}.json"
    if not path.exists():
        return None
    try:
        return Aim.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, ValueError) as e:
        log.warning("get_aim: corrupted aim file %s: %s", path, e)
        return None


def get_aim(aim_id: str) -> Aim | None:
    client = _get_fs_client()
    if client is not None:
        try:
            snap = client.collection("aims").document(aim_id).get()
            if snap.exists:
                log.info("[firestore] read aims/%s (hit)", aim_id)
                return Aim.model_validate(snap.to_dict())
            log.warning("[firestore] read aims/%s miss, falling back to local JSON", aim_id)
        except Exception as e:  # noqa: BLE001
            log.warning("[firestore] get_aim %s errored, falling back to local: %s", aim_id, e)
    return _get_aim_local(aim_id)


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
    # Local delete
    path = AIMS_DIR / f"{aim_id}.json"
    local_existed = path.exists()
    path.unlink(missing_ok=True)

    # Firestore delete — counted even if only Firestore had the doc, so that a
    # Cloud-Run-written doc still reports as deleted from a laptop CLI call.
    fs_existed = False
    client = _get_fs_client()
    if client is not None:
        try:
            ref = client.collection("aims").document(aim_id)
            snap = ref.get()
            if snap.exists:
                fs_existed = True
            ref.delete()
            log.info("[firestore] deleted aims/%s (existed=%s)", aim_id, fs_existed)
        except Exception as e:  # noqa: BLE001
            log.warning("[firestore] delete_aim %s failed: %s", aim_id, e)

    return local_existed or fs_existed


def _list_aims_for_user_local(user_id: str) -> list[Aim]:
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


def list_aims_for_user(user_id: str) -> list[Aim]:
    client = _get_fs_client()
    if client is not None:
        try:
            from google.cloud.firestore_v1.base_query import FieldFilter  # type: ignore

            query = client.collection("aims").where(
                filter=FieldFilter("user_id", "==", user_id)
            )
            aims: list[Aim] = []
            for snap in query.stream():
                try:
                    aims.append(Aim.model_validate(snap.to_dict()))
                except ValueError as e:
                    log.warning("[firestore] list_aims_for_user: skipping bad doc %s: %s", snap.id, e)
            if aims:
                aims.sort(key=lambda a: a.created_at, reverse=True)
                log.info("[firestore] list_aims_for_user user=%s → %d", user_id, len(aims))
                return aims
            log.warning("[firestore] list_aims_for_user user=%s empty, falling back to local", user_id)
        except Exception as e:  # noqa: BLE001
            log.warning("[firestore] list_aims_for_user errored, falling back to local: %s", e)
    return _list_aims_for_user_local(user_id)


# ---------------------------------------------------------------------------
# Digests
# ---------------------------------------------------------------------------


def save_digest(digest: Digest) -> None:
    _atomic_write(DIGESTS_DIR / f"{digest.digest_id}.json", _dump(digest))
    client = _get_fs_client()
    if client is not None:
        try:
            client.collection("digests").document(digest.digest_id).set(
                digest.model_dump(mode="json")
            )
            log.info("[firestore] wrote digests/%s", digest.digest_id)
        except Exception as e:  # noqa: BLE001
            log.warning("[firestore] save_digest %s failed: %s", digest.digest_id, e)


def _get_digest_local(digest_id: str) -> Digest | None:
    path = DIGESTS_DIR / f"{digest_id}.json"
    if not path.exists():
        return None
    try:
        return Digest.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, ValueError) as e:
        log.warning("get_digest: corrupted digest file %s: %s", path, e)
        return None


def get_digest(digest_id: str) -> Digest | None:
    client = _get_fs_client()
    if client is not None:
        try:
            snap = client.collection("digests").document(digest_id).get()
            if snap.exists:
                log.info("[firestore] read digests/%s (hit)", digest_id)
                return Digest.model_validate(snap.to_dict())
            log.warning("[firestore] read digests/%s miss, falling back to local JSON", digest_id)
        except Exception as e:  # noqa: BLE001
            log.warning("[firestore] get_digest %s errored, falling back to local: %s", digest_id, e)
    return _get_digest_local(digest_id)


# ---------------------------------------------------------------------------
# Raw articles + dedup seen-set — JSON only (local remains dedup truth)
# ---------------------------------------------------------------------------


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
