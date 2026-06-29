"""Cloudflare R2 連携: 死活監視ハートビート＋DBバックアップ（S3互換API）。

認証情報（環境変数）が無ければ全関数は安全に no-op（ローカル動作を妨げない）。
  R2_ACCOUNT_ID / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY  … 必須
  R2_BUCKET（既定 rakkoma-analyzer）/ R2_ENDPOINT（任意・通常 account_id から自動）

設計:
  - put_heartbeat: heartbeat/observer.json を毎ポーリングで上書き（生死信号＋統計）。
  - daily_backup : 当日のローカル整合スナップショットを作り（稼働中でも壊れない backup API）、
                   R2へ未アップなら上げる。ローカル/R2 とも直近 BACKUP_RETENTION 日分のみ保持。
  R2 はエグレス無料なので、クラウド側（別のClaude等）が heartbeat を読んで生死判定できる。
"""

import json
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config import (
    R2_BUCKET_DEFAULT, R2_HEARTBEAT_KEY, R2_BACKUP_PREFIX, BACKUP_RETENTION,
)


def _bucket() -> str:
    return os.environ.get("R2_BUCKET") or R2_BUCKET_DEFAULT


def _client():
    """R2 の S3 クライアント。認証情報 or boto3 が無ければ None（=機能オフ）。"""
    ak = os.environ.get("R2_ACCESS_KEY_ID")
    sk = os.environ.get("R2_SECRET_ACCESS_KEY")
    acct = os.environ.get("R2_ACCOUNT_ID")
    endpoint = os.environ.get("R2_ENDPOINT") or (
        f"https://{acct}.r2.cloudflarestorage.com" if acct else None)
    if not (ak and sk and endpoint):
        return None
    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        return None
    return boto3.client(
        "s3", endpoint_url=endpoint,
        aws_access_key_id=ak, aws_secret_access_key=sk,
        region_name="auto",
        config=Config(retries={"max_attempts": 3, "mode": "standard"},
                      connect_timeout=10, read_timeout=60),
    )


def is_configured() -> bool:
    return _client() is not None


# ── ハートビート（死活監視）──────────────────────────────────────────────────

def put_heartbeat(stats: dict) -> bool:
    """heartbeat/observer.json を上書き。R2未設定なら no-op で False。"""
    cli = _client()
    if cli is None:
        return False
    try:
        cli.put_object(
            Bucket=_bucket(), Key=R2_HEARTBEAT_KEY,
            Body=json.dumps(stats, ensure_ascii=False).encode("utf-8"),
            ContentType="application/json",
        )
        return True
    except Exception:
        return False


# ── DBバックアップ（ローカル整合スナップショット＋R2）─────────────────────────

def _local_snapshot(db_path, backups_dir, today) -> Path:
    """稼働中でも整合する SQLite online backup でローカルへ当日分を作る（既存ならそのまま）。"""
    backups_dir = Path(backups_dir)
    backups_dir.mkdir(parents=True, exist_ok=True)
    dst = backups_dir / f"rakkoma-{today}.db"
    if not dst.exists():
        src = sqlite3.connect(str(db_path))
        bak = sqlite3.connect(str(dst))
        try:
            with bak:
                src.backup(bak)
        finally:
            bak.close()
            src.close()
    return dst


def _prune_local(backups_dir, keep):
    files = sorted(Path(backups_dir).glob("rakkoma-*.db"))
    for f in files[:-keep] if len(files) > keep else []:
        try:
            f.unlink()
        except OSError:
            pass


def _prune_r2(cli, keep):
    try:
        resp = cli.list_objects_v2(Bucket=_bucket(), Prefix=R2_BACKUP_PREFIX)
        keys = sorted(o["Key"] for o in resp.get("Contents", []) if o["Key"].endswith(".db"))
        for k in keys[:-keep] if len(keys) > keep else []:
            cli.delete_object(Bucket=_bucket(), Key=k)
    except Exception:
        pass


def daily_backup(db_path, backups_dir, today, retention=BACKUP_RETENTION) -> dict:
    """当日分のローカルスナップショットを作り、R2設定時は未アップ分を上げる。
    冪等（当日分があればスキップ）。返り値 {'local': bool, 'r2': bool}。"""
    out = {"local": False, "r2": False}
    dst = _local_snapshot(db_path, backups_dir, today)
    out["local"] = dst.exists()
    _prune_local(backups_dir, retention)

    cli = _client()
    if cli is None:
        return out
    key = f"{R2_BACKUP_PREFIX}rakkoma-{today}.db"
    try:
        already = True
        try:
            cli.head_object(Bucket=_bucket(), Key=key)
        except Exception:
            already = False
        if not already:
            cli.upload_file(str(dst), _bucket(), key)
        out["r2"] = True
        _prune_r2(cli, retention)
    except Exception:
        pass
    return out
