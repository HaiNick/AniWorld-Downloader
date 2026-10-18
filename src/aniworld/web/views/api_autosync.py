"""AutoSync status, the tracked series and the manual Sync now trigger."""

import threading

from flask import abort, jsonify, request

from ...logger import get_logger
from .. import autosync, db
from ..settings_store import autosync_enabled

logger = get_logger(__name__)


def register(bp):
    bp.add_url_rule("/autosync/status", view_func=autosync_status)
    bp.add_url_rule("/autosync/run", view_func=autosync_run, methods=["POST"])
    bp.add_url_rule("/autosync/series", view_func=list_series)
    bp.add_url_rule("/autosync/series", view_func=add_series, methods=["POST"])
    bp.add_url_rule(
        "/autosync/series/<int:series_id>",
        view_func=update_series,
        methods=["PUT"],
    )
    bp.add_url_rule(
        "/autosync/series/<int:series_id>",
        view_func=delete_series,
        methods=["DELETE"],
    )
    bp.add_url_rule(
        "/autosync/series/<int:series_id>/run",
        view_func=run_series,
        methods=["POST"],
    )
    # The "Sync this" checkbox in the download modal
    bp.add_url_rule("/autosync/tracked", view_func=tracked_state)
    bp.add_url_rule("/autosync/tracked", view_func=set_tracked_state, methods=["POST"])


def _guard():
    if not autosync_enabled():
        abort(404)


def autosync_status():
    _guard()
    return jsonify(autosync.status())


def _start(rows, mark_global):
    """Kick off a cycle in the background so the request returns immediately."""
    if autosync.is_running():
        return jsonify({"error": "A sync is already running"}), 409

    threading.Thread(
        target=_run_quietly,
        args=(rows, mark_global),
        name="aniworld-autosync-manual",
        daemon=True,
    ).start()
    return jsonify({"ok": True, "started": True})


def autosync_run():
    _guard()
    return _start(None, True)


def _run_quietly(rows, mark_global):
    try:
        autosync.run_cycle(rows, mark_global=mark_global)
    except RuntimeError as exc:
        logger.info("AutoSync manual run skipped: %s", exc)
    except Exception:
        logger.exception("AutoSync manual run failed")


# ---------------------------------------------------------------------------
# Tracked series
# ---------------------------------------------------------------------------
def _settings_from(data):
    try:
        return autosync.clean_settings(data), None
    except autosync.SettingsError as exc:
        return None, (jsonify({"error": str(exc)}), 400)


def list_series():
    _guard()
    rows = db.get_autosync_series()
    return jsonify({"series": [autosync.series_status(row) for row in rows]})


def add_series():
    _guard()
    data = request.get_json(silent=True) or {}
    series_url = (data.get("series_url") or "").strip()
    if not series_url:
        return jsonify({"error": "series_url is required"}), 400
    if not data.get("language") and not db.get_autosync_series_by_url(series_url):
        return jsonify({"error": "language is required"}), 400

    fields, error = _settings_from(data)
    if error:
        return error
    row = db.save_autosync_series(series_url, **fields)
    return jsonify({"ok": True, "series": autosync.series_status(row)})


def _existing(series_id):
    row = db.get_autosync_series_by_id(series_id)
    if not row:
        abort(404)
    return row


def update_series(series_id):
    _guard()
    row = _existing(series_id)
    fields, error = _settings_from(request.get_json(silent=True) or {})
    if error:
        return error
    row = db.save_autosync_series(row["series_url"], **fields)
    return jsonify({"ok": True, "series": autosync.series_status(row)})


def delete_series(series_id):
    _guard()
    db.remove_autosync_series(series_id=series_id)
    return jsonify({"ok": True})


def run_series(series_id):
    """Check one series now, without moving the global schedule."""
    _guard()
    return _start([_existing(series_id)], False)


# ---------------------------------------------------------------------------
# Download modal "Sync this" checkbox
# ---------------------------------------------------------------------------
def tracked_state():
    _guard()
    series_url = (request.args.get("url") or "").strip()
    if not series_url:
        return jsonify({"error": "url is required"}), 400
    row = db.get_autosync_series_by_url(series_url)
    return jsonify(
        {
            "tracked": row is not None,
            "series": autosync.series_status(row) if row else None,
        }
    )


def set_tracked_state():
    """Tick or untick "Sync this", with the settings chosen next to it."""
    _guard()
    data = request.get_json(silent=True) or {}
    series_url = (data.get("series_url") or "").strip()
    if not series_url:
        return jsonify({"error": "series_url is required"}), 400

    if not data.get("tracked"):
        db.remove_autosync_series(series_url=series_url)
        return jsonify({"ok": True, "tracked": False})

    if not data.get("language") and not db.get_autosync_series_by_url(series_url):
        return jsonify({"error": "language is required"}), 400
    fields, error = _settings_from(data)
    if error:
        return error
    row = db.save_autosync_series(series_url, **fields)
    return jsonify({"ok": True, "tracked": True, "series": autosync.series_status(row)})
