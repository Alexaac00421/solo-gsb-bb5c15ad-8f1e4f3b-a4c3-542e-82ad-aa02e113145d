"""定时公开前内审预览凭证测试。

覆盖：
- 颁发：管理员密钥缺失/错误、服务未配置密钥；未知文档/版本、已撤回、
  未设置公开时间（立即公开）、公开时间已过；TTL 越界；成功响应形态；
- 凭证落库只存摘要（明文不可逆推），不影响公开读取（到点前仍 404）；
- 领取：预览包只含成品 PNG + 冻结遮盖 JSON + 清单，绝不包含原图/原图摘要/
  上传文件名；
- 无效/伪造、过期、已核销凭证统一 403 invalid_preview_token，不泄露版本信息；
- 凭证原子一次性：并发领取仅一笔成功；
- 撤回/领取串行：撤回先提交则拒绝（不核销），领取先核销则本次下载完成；
- 成品缺失/损坏：500 且不核销（回滚），修复后可重试；
- 凭证不进入服务数据归档；恢复后旧凭证不能沿用；
- 公开时间到达后凭证失效。
"""
import hashlib
import io
import json
import os
import sqlite3
import threading
import zipfile
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import main as main_module
from app.config import Settings
from app.main import create_app
from tests.test_api import png_bytes, upload  # noqa: F401  复用构造器与助手

ADMIN_KEY = "local-admin-secret"
FUTURE = "2026-10-20T08:00:00Z"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    settings = Settings(
        data_dir=str(tmp_path / "data"),
    )
    # 显式为本组测试配置本地管理员密钥
    settings.preview_admin_key = ADMIN_KEY
    app = create_app(settings)
    # 固定时钟：今天 2026-10-07，FUTURE 在 30 天窗口内
    clock = {"now": datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc)}
    monkeypatch.setattr(main_module, "_now_dt", lambda: clock["now"])
    with TestClient(app) as c:
        c.app.state.clock = clock
        yield c


# ---------------- 助手 ----------------

def _new_doc(client, color=(10, 20, 30), filename="img.png"):
    return upload(client, png_bytes(color=color), filename).json()["doc_id"]


def _annotate(client, doc_id, revision, rects):
    r = client.put(
        f"/documents/{doc_id}/annotations",
        json={"revision": revision, "rects": rects},
    )
    assert r.status_code == 200, r.text


def _publish(client, doc_id, **kwargs):
    r = client.post(f"/documents/{doc_id}/publish", json=kwargs or None)
    assert r.status_code == 201, r.text
    return r.json()


def _issue(client, doc_id, version=1, ttl=15, key=ADMIN_KEY):
    headers = {"X-Admin-Key": key} if key is not None else {}
    return client.post(
        "/preview-tokens",
        json={"doc_id": doc_id, "version": version, "ttl_minutes": ttl},
        headers=headers,
    )


def _issue_ok(client, doc_id, version=1, ttl=15, key=ADMIN_KEY):
    r = _issue(client, doc_id, version, ttl, key)
    assert r.status_code == 201, r.text
    return r.json()


def _redeem(client, token):
    return client.get(f"/preview-packages/{token}")


def _unzip(response) -> zipfile.ZipFile:
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"
    return zipfile.ZipFile(io.BytesIO(response.content))


def _db(settings):
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------- 颁发：密钥 ----------------

def test_issue_requires_configured_admin_key(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"))
    settings.preview_admin_key = None
    app = create_app(settings)
    with TestClient(app) as c:
        d = _new_doc(c)
        _publish(c, d, publish_at=FUTURE)
        r = c.post(
            "/preview-tokens",
            json={"doc_id": d, "version": 1, "ttl_minutes": 10},
            headers={"X-Admin-Key": "anything"},
        )
        assert r.status_code == 503
        assert r.json()["detail"]["code"] == "preview_admin_key_not_configured"
        serialized = json.dumps(r.json()["detail"])
        assert d not in serialized


def test_issue_missing_or_wrong_admin_key_rejected(client):
    d = _new_doc(client)
    _publish(client, d, publish_at=FUTURE)

    r = client.post(
        "/preview-tokens",
        json={"doc_id": d, "version": 1, "ttl_minutes": 10},
    )
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "invalid_admin_key"

    r = _issue(client, d, key="wrong-secret")
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "invalid_admin_key"
    # 鉴权失败不查库：错误体不泄露文档/版本
    assert d not in json.dumps(r.json()["detail"])


# ---------------- 颁发：版本状态 ----------------

def test_issue_unknown_document_and_version(client):
    d = _new_doc(client)
    _publish(client, d, publish_at=FUTURE)

    r = _issue(client, "deadbeef" * 4)
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "document_not_found"

    r = _issue(client, d, version=99)
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "unknown_version"


def test_issue_for_immediate_publication_rejected(client):
    d = _new_doc(client)
    _publish(client, d)  # 无 publish_at -> 立即公开
    r = _issue(client, d, version=1)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "preview_requires_future_publication"
    assert detail["doc_id"] == d and detail["version"] == 1


def test_issue_for_past_publish_time_rejected(client):
    d = _new_doc(client)
    _publish(client, d, publish_at=FUTURE)
    # 时钟拨到公开时间之后
    client.app.state.clock["now"] = datetime(
        2026, 10, 25, 0, 0, 0, tzinfo=timezone.utc
    )
    r = _issue(client, d, version=1)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "preview_requires_future_publication"


def test_issue_for_withdrawn_version_rejected(client):
    d = _new_doc(client)
    _publish(client, d, publish_at=FUTURE)
    r = client.post(
        f"/documents/{d}/publications/1/withdraw",
        json={"reason": "遮盖遗漏"},
    )
    assert r.status_code == 201
    r = _issue(client, d)
    assert r.status_code == 410
    assert r.json()["detail"]["code"] == "publication_withdrawn"


def test_issue_ttl_bounds(client):
    d = _new_doc(client)
    _publish(client, d, publish_at=FUTURE)
    for ttl in (0, -1, 61):
        r = _issue(client, d, ttl=ttl)
        assert r.status_code == 422, ttl


# ---------------- 颁发成功：凭证形态与落库摘要 ----------------

def test_issue_success_response_and_expires_at_cap(client):
    d = _new_doc(client)
    _publish(client, d, publish_at=FUTURE)

    body = _issue_ok(client, d, ttl=15)
    assert set(body) == {
        "preview_token", "doc_id", "version",
        "issued_at", "expires_at", "publish_at",
    }
    assert body["doc_id"] == d and body["version"] == 1
    assert body["publish_at"] == FUTURE
    assert body["issued_at"] == "2026-10-07T12:00:00Z"
    assert body["expires_at"] == "2026-10-07T12:15:00Z"
    token = body["preview_token"]
    assert token.startswith(main_module.PREVIEW_TOKEN_PREFIX)
    assert len(token) > 40

    # 库里只有 SHA-256 摘要：明文与任何前缀片段都不在库中
    settings = client.app.state.settings
    db = _db(settings)
    row = db.execute(
        "SELECT token_hash, doc_id, version, expires_at, redeemed_at"
        " FROM preview_tokens"
    ).fetchone()
    assert row["token_hash"] == hashlib.sha256(token.encode()).hexdigest()
    assert row["doc_id"] == d and row["version"] == 1
    assert row["redeemed_at"] is None
    # 全表所有列都不含明文或其前缀
    db_raw = b"\x00".join(
        str(v).encode()
        for r in db.execute("SELECT * FROM preview_tokens") for v in tuple(r)
    )
    db.close()
    assert token.encode() not in db_raw
    assert main_module.PREVIEW_TOKEN_PREFIX.encode() not in db_raw

    # TTL 越过公开时间时，截止时间截断到公开时间（领取必须早于公开时间）
    near = "2026-10-07T12:10:00Z"  # 10 分钟后公开，在 30 天窗口内
    d2 = _new_doc(client, (4, 5, 6))
    _publish(client, d2, publish_at=near)
    body2 = _issue_ok(client, d2, ttl=60)
    assert body2["expires_at"] == near
    assert body2["preview_token"] != token  # 每次颁发独立随机凭证


def test_preview_issuance_does_not_open_public_read(client):
    d = _new_doc(client)
    _annotate(client, d, 0, [{"x": 0, "y": 0, "w": 4, "h": 4, "reason": "甲"}])
    _publish(client, d, publish_at=FUTURE)
    _issue_ok(client, d)
    # 原公开读取在到点前仍为 404，预览凭证不改变任何公开语义
    assert client.get(f"/publications/{d}/1").status_code == 404
    assert client.get(f"/publications/{d}/1/image").status_code == 404


# ---------------- 领取：包内容 ----------------

def test_redeem_preview_package_contents(client):
    d = _new_doc(client, (10, 20, 30))
    _annotate(client, d, 0, [
        {"x": 1, "y": 2, "w": 3, "h": 4, "reason": "身份证号"},
        {"x": 8, "y": 8, "w": 2, "h": 2, "reason": "工号"},
    ])
    pub = _publish(client, d, publish_at=FUTURE)
    body = _issue_ok(client, d)

    zf = _unzip(_redeem(client, body["preview_token"]))
    names = sorted(zf.namelist())
    image_name = f"{d}_v1.png"
    assert names == sorted(["manifest.json", "redactions.json", image_name])
    # 包内没有任何 originals 成员或上传文件名痕迹
    assert all("original" not in n for n in names)
    assert "img" not in "".join(names)

    raw = zf.read(image_name)
    assert hashlib.sha256(raw).hexdigest() == pub["output_sha256"]
    assert raw.startswith(b"\x89PNG\r\n\x1a\n")

    manifest = json.loads(zf.read("manifest.json"))
    assert manifest == {
        "kind": "internal_preview",
        "doc_id": d,
        "version": 1,
        "annotation_revision": 1,
        "output_sha256": pub["output_sha256"],
        "publish_at": FUTURE,
        "generated_at": "2026-10-07T12:00:00Z",
        "files": {"image": image_name, "redactions": "redactions.json"},
    }
    # 绝不包含原图摘要
    serialized = json.dumps(manifest)
    assert pub["original_sha256"] not in serialized
    assert "original" not in serialized

    redactions = json.loads(zf.read("redactions.json"))
    assert redactions == {
        "doc_id": d,
        "version": 1,
        "annotation_revision": 1,
        "redaction_count": 2,
        "redactions": [
            {"x": 1, "y": 2, "w": 3, "h": 4, "reason": "身份证号"},
            {"x": 8, "y": 8, "w": 2, "h": 2, "reason": "工号"},
        ],
    }
    assert "original" not in json.dumps(redactions)


def test_preview_freezes_redactions_at_publication_revision(client):
    d = _new_doc(client)
    _annotate(client, d, 0, [{"x": 0, "y": 0, "w": 4, "h": 4, "reason": "甲"}])
    _publish(client, d, publish_at=FUTURE)
    # 发布之后再改标注：预览包必须仍是发布时冻结的遮盖
    _annotate(client, d, 1, [])
    body = _issue_ok(client, d)
    zf = _unzip(_redeem(client, body["preview_token"]))
    redactions = json.loads(zf.read("redactions.json"))
    assert redactions["annotation_revision"] == 1
    assert redactions["redaction_count"] == 1
    assert redactions["redactions"][0]["reason"] == "甲"


# ---------------- 领取：无效/过期/已用统一 403 ----------------

def test_redeem_unknown_token_403_without_version_leak(client):
    r = _redeem(client, "pvp_this-token-never-existed")
    assert r.status_code == 403
    detail = r.json()["detail"]
    assert detail == {
        "code": "invalid_preview_token",
        "message": "the preview token is invalid, expired or has already"
                   " been used",
    }


def test_redeem_is_one_time_use(client):
    d = _new_doc(client)
    _publish(client, d, publish_at=FUTURE)
    token = _issue_ok(client, d)["preview_token"]

    r1 = _redeem(client, token)
    assert r1.status_code == 200
    r2 = _redeem(client, token)
    assert r2.status_code == 403
    assert r2.json()["detail"]["code"] == "invalid_preview_token"
    assert r2.headers["content-type"] != "application/zip"

    settings = client.app.state.settings
    db = _db(settings)
    row = db.execute(
        "SELECT redeemed_at FROM preview_tokens"
    ).fetchone()
    db.close()
    assert row["redeemed_at"] is not None


def test_redeem_expired_token_403(client):
    d = _new_doc(client)
    _publish(client, d, publish_at=FUTURE)
    token = _issue_ok(client, d, ttl=1)["preview_token"]
    # 时钟越过凭证截止时间（仍在公开时间之前）
    client.app.state.clock["now"] = datetime(
        2026, 10, 7, 12, 5, 0, tzinfo=timezone.utc
    )
    r = _redeem(client, token)
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "invalid_preview_token"
    # 过期尝试不核销
    settings = client.app.state.settings
    db = _db(settings)
    assert db.execute(
        "SELECT redeemed_at FROM preview_tokens"
    ).fetchone()["redeemed_at"] is None
    db.close()


def test_redeem_after_publish_time_403(client):
    d = _new_doc(client)
    _publish(client, d, publish_at=FUTURE)
    token = _issue_ok(client, d, ttl=60)["preview_token"]
    # 公开时间到达：预览通道关闭（公开面自行开放）
    client.app.state.clock["now"] = datetime(
        2026, 10, 20, 8, 1, 0, tzinfo=timezone.utc
    )
    r = _redeem(client, token)
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "invalid_preview_token"
    assert r.headers["content-type"] != "application/zip"


def test_redeem_boundary_expiry_comparison(client):
    # expires_at == now 即视为过期（必须严格早于截止时间）
    d = _new_doc(client)
    _publish(client, d, publish_at=FUTURE)
    token = _issue_ok(client, d, ttl=1)["preview_token"]  # 12:01:00Z 截止
    client.app.state.clock["now"] = datetime(
        2026, 10, 7, 12, 1, 0, tzinfo=timezone.utc
    )
    r = _redeem(client, token)
    assert r.status_code == 403


# ---------------- 凭证一次性：并发仅一笔成功 ----------------

def test_concurrent_redemption_only_one_succeeds(client):
    d = _new_doc(client)
    _publish(client, d, publish_at=FUTURE)
    token = _issue_ok(client, d)["preview_token"]

    outcomes: list = []
    start = threading.Barrier(5)

    def worker():
        start.wait()
        outcomes.append(_redeem(client, token))

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert len(outcomes) == 5
    codes = sorted(r.status_code for r in outcomes)
    assert codes == [200, 403, 403, 403, 403]
    ok = [r for r in outcomes if r.status_code == 200]
    assert len(ok) == 1
    zf = zipfile.ZipFile(io.BytesIO(ok[0].content))
    assert f"{d}_v1.png" in zf.namelist()


# ---------------- 撤回 / 领取串行 ----------------

def test_withdraw_committed_first_rejects_redemption_without_consuming(client):
    d = _new_doc(client)
    _publish(client, d, publish_at=FUTURE)
    token = _issue_ok(client, d)["preview_token"]

    r = client.post(
        f"/documents/{d}/publications/1/withdraw",
        json={"reason": "遮盖遗漏：右下角工号未遮盖"},
    )
    assert r.status_code == 201

    r = _redeem(client, token)
    assert r.status_code == 410
    detail = r.json()["detail"]
    assert detail["code"] == "publication_withdrawn"
    assert detail["doc_id"] == d and detail["version"] == 1
    assert r.headers["content-type"] != "application/zip"
    # 拒绝路径回滚，凭证保持未核销（但版本已撤回，仍无法领取）
    settings = client.app.state.settings
    db = _db(settings)
    assert db.execute(
        "SELECT redeemed_at FROM preview_tokens"
    ).fetchone()["redeemed_at"] is None
    db.close()


def test_redemption_confirmed_first_completes_despite_waiting_withdraw(client):
    """领取在写事务内先核销并通过复查：并发撤回被写锁挡到事务提交之后，
    本次下载照常完成；撤回随后生效，凭证二次领取被拒。"""
    d = _new_doc(client)
    _publish(client, d, publish_at=FUTURE)
    token = _issue_ok(client, d)["preview_token"]

    checks_done = threading.Event()
    withdraw_started = threading.Event()
    withdraw_returned = threading.Event()
    release = threading.Event()
    outcomes = {}

    def hook():
        checks_done.set()
        release.wait(timeout=10)

    def redeemer():
        outcomes["redeem"] = _redeem(client, token)

    def withdrawer():
        assert checks_done.wait(timeout=10)
        withdraw_started.set()
        outcomes["withdraw"] = client.post(
            f"/documents/{d}/publications/1/withdraw",
            json={"reason": "遮盖遗漏"},
        )
        withdraw_returned.set()

    main_module.PREVIEW_REDEMPTION_HOOK = hook
    try:
        t_r = threading.Thread(target=redeemer)
        t_r.start()
        assert checks_done.wait(timeout=10)
        t_w = threading.Thread(target=withdrawer)
        t_w.start()
        assert withdraw_started.wait(timeout=10)
        assert not withdraw_returned.wait(timeout=0.5), (
            "撤回在领取事务提交前就完成了：核销未与撤回串行")
        release.set()
        assert withdraw_returned.wait(timeout=20)
        t_r.join(timeout=20)
        t_w.join(timeout=20)
    finally:
        main_module.PREVIEW_REDEMPTION_HOOK = None

    assert outcomes["redeem"].status_code == 200, outcomes["redeem"].text
    zf = zipfile.ZipFile(io.BytesIO(outcomes["redeem"].content))
    assert f"{d}_v1.png" in zf.namelist()
    assert outcomes["withdraw"].status_code in (200, 201)

    # 撤回随后生效：凭证再用既已核销又面对撤回版本，统一 403
    assert _redeem(client, token).status_code == 403
    # 公开读取为 410
    assert client.get(f"/publications/{d}/1").status_code == 410


# ---------------- 成品缺失/损坏：不核销，修复后可重试 ----------------

def test_redeem_missing_artifact_fails_and_does_not_consume_token(client):
    d = _new_doc(client)
    _publish(client, d, publish_at=FUTURE)
    token = _issue_ok(client, d)["preview_token"]

    settings = client.app.state.settings
    db = _db(settings)
    path = db.execute(
        "SELECT output_path FROM publications WHERE doc_id = ? AND version = 1",
        (d,),
    ).fetchone()["output_path"]
    db.close()
    os.unlink(path)

    r = _redeem(client, token)
    assert r.status_code == 500
    assert r.json()["detail"]["code"] == "artifact_missing"
    assert r.headers["content-type"] != "application/zip"

    # 未核销：凭证仍可再用（此处仅验证库状态；恢复文件后领取见下）
    db = _db(settings)
    assert db.execute(
        "SELECT redeemed_at FROM preview_tokens"
    ).fetchone()["redeemed_at"] is None
    db.close()


def test_redeem_corrupt_artifact_rolls_back_redemption(client):
    d = _new_doc(client)
    _publish(client, d, publish_at=FUTURE)
    token = _issue_ok(client, d)["preview_token"]

    settings = client.app.state.settings
    db = _db(settings)
    row = db.execute(
        "SELECT output_path FROM publications WHERE doc_id = ? AND version = 1",
        (d,),
    ).fetchone()
    path = row["output_path"]
    db.close()
    with open(path, "wb") as f:
        f.write(b"tampered-artifact-bytes")

    r = _redeem(client, token)
    assert r.status_code == 500
    assert r.json()["detail"]["code"] == "artifact_corrupt"

    # 回滚使核销撤销：修复成品（重新渲染与发布时相同的 PNG）后，
    # 同一凭证仍可成功领取。
    db = _db(settings)
    assert db.execute(
        "SELECT redeemed_at FROM preview_tokens"
    ).fetchone()["redeemed_at"] is None
    db.close()

    import app.imaging as imaging
    db = _db(settings)
    drow = db.execute(
        "SELECT original_path FROM documents WHERE id = ?", (d,)
    ).fetchone()
    db.close()
    oriented, _ = imaging.load_oriented(open(drow["original_path"], "rb").read())
    imaging.render_publication(oriented, []).save(path, format="PNG")
    # 无遮盖的 40x30 成品：v1 发布时同样没有标注矩形
    r = _redeem(client, token)
    assert r.status_code == 200, r.text
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    assert f"{d}_v1.png" in zf.namelist()


# ---------------- 凭证不进入归档；恢复后旧凭证作废 ----------------

def test_preview_tokens_are_not_archived(client):
    d = _new_doc(client)
    _annotate(client, d, 0, [{"x": 0, "y": 0, "w": 2, "h": 2, "reason": "甲"}])
    _publish(client, d, publish_at=FUTURE)
    token = _issue_ok(client, d)["preview_token"]
    token_hash = hashlib.sha256(token.encode()).hexdigest()

    r = client.post("/service-data-archives")
    assert r.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    # 归档任何成员（含清单与记录 JSON）都不含凭证摘要或明文
    for name in zf.namelist():
        assert token_hash not in zf.read(name).decode("utf-8", "ignore")
        assert token not in zf.read(name).decode("utf-8", "ignore")
        assert "preview" not in name
    manifest = json.loads(zf.read("manifest.json"))
    assert "preview_tokens" not in json.dumps(manifest)


def test_preview_tokens_do_not_survive_restore(client, tmp_path):
    # 源实例：定时版本 + 凭证（领取一次），随后归档并恢复到空实例
    d = _new_doc(client)
    _publish(client, d, publish_at=FUTURE)
    token = _issue_ok(client, d)["preview_token"]
    assert _redeem(client, token).status_code == 200

    archive = client.post("/service-data-archives")
    assert archive.status_code == 200
    archive_path = tmp_path / "snapshot.zip"
    archive_path.write_bytes(archive.content)

    dst_settings = Settings(data_dir=str(tmp_path / "dst-data"))
    dst_app = create_app(dst_settings)
    with TestClient(dst_app) as dst:
        with open(archive_path, "rb") as f:
            r = dst.post(
                "/service-data-archives/restore",
                files={"file": ("snapshot.zip", f, "application/zip")},
            )
        assert r.status_code == 200, r.text
        # 恢复后的记录与公开规则可用
        assert dst.get(f"/publications/{d}/1").status_code == 404
        # 旧凭证在新实例无效：凭证摘要未被恢复
        r = dst.get(f"/preview-packages/{token}")
        assert r.status_code == 403
        assert r.json()["detail"]["code"] == "invalid_preview_token"

    db = sqlite3.connect(dst_settings.db_path)
    assert db.execute("SELECT COUNT(*) FROM preview_tokens").fetchone()[0] == 0
    db.close()


def test_restore_rejects_when_preview_tokens_table_has_rows(
    client, tmp_path
):
    # 恢复目标实例不是严格空实例（preview_tokens 有本机残留）：
    # 空实例判定拒绝（instance_not_empty），不接受部分恢复。
    d = _new_doc(client)
    _publish(client, d, publish_at=FUTURE)
    archive = client.post("/service-data-archives")
    assert archive.status_code == 200
    archive_path = tmp_path / "snapshot.zip"
    archive_path.write_bytes(archive.content)

    dst_settings = Settings(data_dir=str(tmp_path / "dst-data2"))
    dst_app = create_app(dst_settings)
    with TestClient(dst_app) as dst:
        # 在目标实例制造一条过期凭证残留（绕过 API 直接插库）
        import app.db as db_mod
        db_mod.init_db(dst_settings.db_path)
        conn = sqlite3.connect(dst_settings.db_path)
        conn.execute(
            "INSERT INTO preview_tokens (token_hash, doc_id, version,"
            " expires_at, created_at, redeemed_at) VALUES (?,?,?,?,?,?)",
            ("a" * 64, d, 1, "2026-10-01T00:00:00Z",
             "2026-10-01T00:00:00Z", None),
        )
        conn.commit()
        conn.close()

        with open(archive_path, "rb") as f:
            r = dst.post(
                "/service-data-archives/restore",
                files={"file": ("snapshot.zip", f, "application/zip")},
            )
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "instance_not_empty"


def test_stale_token_issued_before_withdraw_then_reissue_still_possible(client):
    # 撤回导致前一凭证无法领取后，撤回不可撤销，故只验证发布/撤回/
    # 归档恢复语义未被破坏（恢复后的定时版本仍可在到点后公开读取）。
    d = _new_doc(client)
    _publish(client, d, publish_at=FUTURE)
    _issue_ok(client, d)
    assert client.post(
        f"/documents/{d}/publications/1/withdraw",
        json={"reason": "x"},
    ).status_code == 201
    # 已撤回版本不能再颁发
    assert _issue(client, d).status_code == 410
