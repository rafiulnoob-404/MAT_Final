"""Health, cache reload and storage synchronization routes."""

from flask import Blueprint, jsonify

try:
    import psutil
except ImportError:
    psutil = None


def create_blueprint(*, github_enabled, repo, branch, path, token_set, sync_secs,
                     gemini_env, gemini_model, pending, ignored_files,
                     flush, reload_caches, reload_storage):
    bp = Blueprint("health", __name__)

    @bp.get("/healthz")
    def healthz():
        return "ok", 200

    @bp.post("/api/reload")
    def reload_cache():
        flush()
        reload_storage()
        reload_caches()
        return jsonify(ok=True)

    @bp.post("/api/sync")
    def sync_now():
        return jsonify(ok=True, pending=flush() if github_enabled else 0)

    @bp.get("/api/health")
    def health():
        legacy = ignored_files()
        return jsonify(github=github_enabled, repo=repo, branch=branch, path=path,
                       token_set=token_set, pending=pending(), sync_secs=sync_secs,
                       gemini_env=gemini_env(), gemini_model=gemini_model,
                       ignored_files=len(legacy), ignored_sample=legacy[:5])

    @bp.get("/system/health")
    def health_system():
        if psutil is None:
            return jsonify(status="unavailable", error="psutil not installed"), 501
        ram = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        healthy = ram.percent < 90 and disk.percent < 90
        return jsonify(status="healthy" if healthy else "warning",
                       cpu_usage_pct=psutil.cpu_percent(interval=None),
                       ram_used_pct=ram.percent,
                       disk_free_mb=round(disk.free / (1024 ** 2), 2)), 200 if healthy else 500

    return bp
