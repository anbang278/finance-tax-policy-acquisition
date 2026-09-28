import json
from argparse import Namespace
from contextlib import nullcontext
from datetime import date
from types import SimpleNamespace

import pytest

from ftr.adapters.chinatax import ChinataxAdapter
from ftr.adapters.common import parse_detail
from ftr.adapters.mof import MofAdapter
from ftr.backup import create_backup, restore_backup, verify_backup
from ftr.cli import parser, run
from ftr.config import load_sources
from ftr.evidence import EvidenceStore
from ftr.models import (
    DecisionRequest,
    DiscoveredRef,
    DocumentRecord,
    DocumentType,
    SemanticDecision,
    TaskRequest,
    digest,
)
from ftr.network import AccessBlocked, BrowserSessionClient, check_url
from ftr.repair import inspect_patch
from ftr.repository import Repository
from ftr.research import Citation, Claim, ResearchDraft, verify_and_render
from ftr.runtime import data_lock


class FakeClient:
    def __init__(self, pages):
        self.pages = pages

    def get(self, url):
        return self.pages[url], url, "text/html"


class FakeTaxResponse:
    def __init__(self, status=200):
        self.status = status
        self.request = SimpleNamespace(
            url="https://www.chinatax.gov.cn/getFileListByCodeId",
            post_data="codeId=&channelId=abc123&page=1&size=10",
            headers={"User-Agent": "browser", "Referer": "https://fgk.chinatax.gov.cn/"},
        )


class FakeTaxClient:
    def __init__(self):
        self.posts = []
        self.cookies = None

    def set_browser_session(self, headers, cookies):
        self.cookies = cookies

    def post_form(self, url, data):
        self.posts.append(data.copy())
        payload = {
            "code": 200,
            "results": {
                "data": {
                    "page": int(data["page"]),
                    "rows": 10,
                    "total": 11,
                    "results": [
                        {
                            "url": "https://www.chinatax.gov.cn/zcfgk/c100012/content.html",
                            "title": "税务文件",
                            "domainMetaList": [
                                {"resultList": [{"key": "writtendate", "value": "2026-09-01"}]}
                            ],
                        }
                    ],
                }
            },
        }
        return json.dumps(payload).encode(), url, "application/json"


class FakeTaxPage:
    url = "about:blank"
    context = SimpleNamespace(cookies=lambda: [{"name": "session", "value": "secret"}])

    def expect_response(self, predicate, timeout):
        class Event:
            value = FakeTaxResponse()

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

        return Event()

    def goto(self, url, **_):
        self.url = url
        return SimpleNamespace(status=200)


def test_tax_listing_date_is_issued_date(monkeypatch):
    monkeypatch.setattr("ftr.adapters.chinatax.check_url", lambda *_: "www.chinatax.gov.cn")
    client = FakeTaxClient()
    adapter = ChinataxAdapter(load_sources().sources["chinatax"], FakeTaxPage(), client)
    refs, next_page, _, _ = adapter.discover(1)
    assert refs[0].listing_date == date(2026, 9, 1)
    assert refs[0].listing_date_kind == "issued_date"
    assert refs[0].url.startswith("https://fgk.chinatax.gov.cn/")
    assert next_page == 2
    assert adapter.discover(2)[1] is None
    assert [post["page"] for post in client.posts] == ["1", "2"]
    assert client.cookies[0]["name"] == "session"


def test_tax_detail_excludes_toolbar_and_marks_main_attachments():
    ref = DiscoveredRef(
        source_id="chinatax",
        url="https://fgk.chinatax.gov.cn/zcfgk/c100012/c5252176/content.html",
        listing_title="税务公告",
        listing_date=date(2026, 9, 4),
        listing_date_kind="issued_date",
        discovered_from="list-hash",
    )
    raw = (
        "<div class='detials contentLeft'><h3>税务公告</h3>"
        "<div class='arctips'>字体：【大】【中】【小】 尚未生效 成文日期：2026-09-04</div>"
        "<div class='article'><div class='arc_cont'><p>公告正文。</p>"
        "<a href='files/form.xls'>申报表</a></div></div></div>"
    ).encode()
    record = parse_detail(ref, raw, digest(raw))
    assert record.body_text == "公告正文。 申报表"
    assert "字体" not in record.body_text
    assert record.issued_date == date(2026, 9, 4)
    assert record.source_status_claim == "尚未生效"
    assert record.attachments[0].content_role == "primary"


def test_tax_denied_browser_session_never_posts(monkeypatch):
    page = FakeTaxPage()
    page.expect_response = lambda *_args, **_kwargs: nullcontext(
        SimpleNamespace(value=FakeTaxResponse(status=403))
    )
    client = FakeTaxClient()
    adapter = ChinataxAdapter(load_sources().sources["chinatax"], page, client)
    with pytest.raises(AccessBlocked):
        adapter.discover(1)
    assert client.posts == []


def test_mof_pagination_and_https_upgrade():
    source = load_sources().sources["mof"]
    first = b'<ul><li><a href="http://kjs.mof.gov.cn/zhengcefabu/202608/t20260805_3994927.htm">Policy</a><span>2026-08-05</span></li></ul><script>var countPage = 2</script>'
    second = b'<ul><li><a href="https://kjs.mof.gov.cn/zhengcefabu/202607/t20260705_3994927.htm">Older</a></li></ul><script>var countPage = 2</script>'
    adapter = MofAdapter(
        source, FakeClient({source.entry: first, source.entry + "index_1.htm": second})
    )
    refs, next_page, _, _ = adapter.discover(1)
    assert next_page == 2
    assert refs[0].url.startswith("https://kjs.mof.gov.cn/")
    assert refs[0].listing_date == date(2026, 8, 5)
    assert adapter.discover(2)[1] is None


def test_type_and_date_are_not_guessed():
    ref = DiscoveredRef(
        source_id="mof",
        url="https://kjs.mof.gov.cn/zhengcefabu/202608/t20260805_3994927.htm",
        listing_title="某准则修订印发",
        listing_date=date(2026, 8, 5),
        listing_date_kind="column_date",
        discovered_from="list-hash",
    )
    raw = "<html><h2>关于印发某准则的通知</h2><div class='TRS_Editor'><p>第一条 正文。</p></div></html>".encode()
    record = parse_detail(ref, raw, digest(raw))
    assert record.document_type == DocumentType.RELEASE_MESSAGE
    assert record.listing_date == date(2026, 8, 5)
    assert record.published_date is None
    assert record.issued_date is None
    assert "第一条" in record.body_text


def test_inline_style_is_not_policy_body():
    ref = DiscoveredRef(
        source_id="mof",
        url="https://kjs.mof.gov.cn/zhengcefabu/202608/t20260805_3994927.htm",
        listing_title="某准则",
        listing_date=date(2026, 8, 5),
        listing_date_kind="column_date",
        discovered_from="list-hash",
    )
    raw = "<div class='TRS_Editor'><style>.TRS_Editor P{color:red}</style><p>正文内容</p></div>".encode()
    record = parse_detail(ref, raw, digest(raw))
    assert record.body_text == "正文内容"


def test_full_notice_with_attached_rules_is_policy_file():
    ref = DiscoveredRef(
        source_id="mof",
        url="https://kjs.mof.gov.cn/zhengcefabu/202608/t20260805_3994927.htm",
        listing_title="印发办法",
        listing_date=date(2026, 8, 5),
        listing_date_kind="column_date",
        discovered_from="list-hash",
    )
    raw = (
        "<h2>关于印发《某办法》的通知</h2><div class='TRS_Editor'><p>第一条 "
        + "采购管理。" * 120
        + "</p></div>"
    ).encode()
    assert parse_detail(ref, raw, digest(raw)).document_type == DocumentType.POLICY_FILE


def test_repository_versions_and_stale_decision(tmp_path):
    repo = Repository(tmp_path)
    request = TaskRequest(
        source_ids=["mof"],
        date_from=date(2026, 1, 1),
        date_to=date(2026, 9, 28),
        idempotency_key="same",
    )
    task_id = repo.create_task(request)
    assert repo.create_task(request) == task_id
    ref = DiscoveredRef(
        source_id="mof",
        url="https://kjs.mof.gov.cn/a",
        listing_title="待重试",
        listing_date_kind="column_date",
        discovered_from="list-hash",
    )
    repo.save_page(task_id, "mof", [ref], None)
    repo.set_ref_state(task_id, "mof", ref.url, "FAILED", "网络中断")
    repo.retry_failed_refs(task_id, "mof")
    assert len(repo.pending_refs(task_id, "mof")) == 1
    with pytest.raises(ValueError):
        repo.create_task(request.model_copy(update={"query": "changed"}))
    record = DocumentRecord(
        source_id="mof",
        source_url="https://kjs.mof.gov.cn/a",
        canonical_url="https://kjs.mof.gov.cn/a",
        listing_title="正文",
        title="正文",
        document_type=DocumentType.POLICY_FILE,
        listing_date_kind="column_date",
        body_text="完整正文",
        body_sha256=digest("完整正文".encode()),
        evidence_id="raw-a",
    )
    record_id, created = repo.add_document(task_id, record)
    assert created
    assert repo.has_document("mof", record.canonical_url)
    assert repo.add_document(task_id, record)[1] is False
    decision = DecisionRequest(
        task_id=task_id,
        record_id=record_id,
        input_digest=digest(record.model_dump_json().encode()),
        excerpt="完整正文",
        evidence_id="raw-a",
    )
    repo.add_decision(decision)
    with pytest.raises(ValueError):
        repo.submit_decision(
            SemanticDecision(
                decision_id=decision.decision_id,
                input_digest="wrong",
                result="PASS",
                reasons=["ok"],
                evidence_ids=["raw-a"],
            )
        )
    accepted = SemanticDecision(
        decision_id=decision.decision_id,
        input_digest=decision.input_digest,
        result="PASS",
        reasons=["已核对"],
        evidence_ids=["raw-a"],
    )
    assert repo.submit_decision(accepted).quality_state == "validated"
    assert repo.submit_decision(accepted).quality_state == "validated"
    draft = ResearchDraft(
        question="正文核验",
        as_of="2026-09-28",
        claims=[
            Claim(
                text="资料包含完整正文",
                kind="原文摘录",
                citations=[Citation(record_id=record_id, evidence_id="raw-a", quote="完整正文")],
            )
        ],
    )
    assert "资料版本" in verify_and_render(repo, draft)
    with pytest.raises(ValueError):
        verify_and_render(
            repo,
            draft.model_copy(
                update={
                    "claims": [
                        Claim(
                            text="错误引用",
                            kind="原文摘录",
                            citations=[
                                Citation(record_id=record_id, evidence_id="raw-a", quote="虚构摘录")
                            ],
                        )
                    ]
                }
            ),
        )
    changed = record.model_copy(update={"record_id": "second", "evidence_id": "raw-b"})
    assert repo.add_document(task_id, changed)[1]
    assert repo.get_document(record_id).evidence_id == "raw-a"
    assert repo.search("完整正文") == []
    repo.close()


def test_patch_guard_rejects_trusted_paths():
    bad = "--- a/tests/test_core.py\n+++ b/tests/test_core.py\n@@ -1 +1 @@\n-x\n+y"
    with pytest.raises(ValueError):
        inspect_patch(bad, "mof")
    good = "--- a/src/ftr/adapters/mof.py\n+++ b/src/ftr/adapters/mof.py\n@@ -1 +1 @@\n-x\n+y"
    assert inspect_patch(good, "mof") == ["src/ftr/adapters/mof.py"]


def test_network_rejects_unlisted_and_private_hosts():
    with pytest.raises(AccessBlocked):
        check_url("https://127.0.0.1/secret", ["127.0.0.1"])
    with pytest.raises(AccessBlocked):
        check_url("https://www.mof.gov.cn.evil.example/", ["www.mof.gov.cn"])
    with pytest.raises(AccessBlocked):
        check_url("http://www.mof.gov.cn/", ["www.mof.gov.cn"])


def test_tax_browser_cookie_transfer_stays_with_tax_hosts():
    client = BrowserSessionClient(["fgk.chinatax.gov.cn", "www.chinatax.gov.cn"])
    try:
        client.set_browser_session(
            {"User-Agent": "browser", "Authorization": "do-not-copy"},
            [
                {"name": "session", "value": "in-memory", "domain": ".chinatax.gov.cn"},
                {"name": "other", "value": "blocked", "domain": ".example.com"},
            ],
        )
        assert client._session.cookies.get("session", domain=".chinatax.gov.cn") == "in-memory"
        assert client._session.cookies.get("other") is None
        assert "Authorization" not in client._session.headers
    finally:
        client.close()


def test_backup_roundtrip_detects_evidence_tamper(tmp_path):
    data_dir = tmp_path / "data"
    repo = Repository(data_dir)
    evidence = EvidenceStore(data_dir).save(
        b"official text", "https://www.mof.gov.cn/a", "https://www.mof.gov.cn/a", "text/plain"
    )
    repo.save_evidence(evidence)
    repo.close()
    backup_dir = tmp_path / "backup"
    assert create_backup(data_dir, backup_dir)["files"]
    assert verify_backup(backup_dir)["verified"]
    restored = tmp_path / "restored"
    restore_backup(backup_dir, restored)
    assert (restored / "evidence" / evidence.relative_path).read_bytes() == b"official text"
    (backup_dir / "evidence" / evidence.relative_path).write_bytes(b"changed")
    with pytest.raises(ValueError):
        verify_backup(backup_dir)
    manifest = json.loads((backup_dir / "manifest.json").read_text())
    manifest["files"]["../outside"] = "fake"
    (backup_dir / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="越界"):
        verify_backup(backup_dir)


def test_pause_can_signal_running_task_without_runtime_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("FTR_DATA_DIR", str(tmp_path))
    repo = Repository(tmp_path)
    task_id = repo.create_task(
        TaskRequest(source_ids=["mof"], date_from=date(2026, 1, 1), date_to=date(2026, 9, 28))
    )
    with data_lock(tmp_path):
        result = run(Namespace(command="task", action="pause", task=task_id))
    assert result.status == "STOP_REQUESTED"
    assert repo.task(task_id)["pause_requested"] == 1
    repo.close()


@pytest.mark.parametrize(
    "arguments",
    [
        ["collect"],
        ["collect", "--date-from", "2026-09-01"],
        ["collect", "--date-to", "2026-09-30"],
    ],
)
def test_collect_requires_both_dates_before_creating_data(tmp_path, monkeypatch, arguments):
    monkeypatch.setenv("FTR_DATA_DIR", str(tmp_path / "data"))
    with pytest.raises(ValueError, match="采集时间范围未明确"):
        run(parser().parse_args(arguments))
    assert not (tmp_path / "data").exists()
