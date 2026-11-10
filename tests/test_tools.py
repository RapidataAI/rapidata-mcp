import asyncio
import time
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from rapidata_mcp import tools
from rapidata_mcp.tools import register_tools

API = "https://api.example.test"
PUBLIC = "https://mcp.example.test"
REF_A = "3f2a9c1e-5b7d-4e2a-9c1f-0d8e7a6b5c4d.png"
REF_B = "0d8e7a6b5c4d4e2a9c1f3f2a9c1e5b7d.mp4"


class FakeAssets:
    def __init__(self) -> None:
        self.imported: list[str] = []

    def upload(self, asset: str) -> SimpleNamespace:
        self.imported.append(asset)
        if "broken" in asset:
            raise RuntimeError("could not download")
        return SimpleNamespace(name=f"ref-of-{asset.rsplit('/', 1)[-1]}")


class FakeDefinition(SimpleNamespace):
    pricing_delay = 0.0

    @property
    def estimated_cost(self) -> SimpleNamespace:
        time.sleep(self.pricing_delay)
        return SimpleNamespace(estimated_cost=0.3049)


class FakeJobs:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.jobs: dict[str, FakeJob] = {}

    def _create(self, **kwargs: Any) -> FakeDefinition:
        self.created.append(kwargs)
        return FakeDefinition(id="jd_1", name=kwargs["name"], _job_details_page="u")

    create_classification_job_definition = _create
    create_compare_job_definition = _create

    def get_job_definition_by_id(self, job_definition_id: str) -> FakeDefinition:
        return FakeDefinition(id=job_definition_id, name="n", _job_details_page="u")

    def get_job_by_id(self, job_id: str) -> "FakeJob":
        return self.jobs[job_id]


class FakeJob:
    def __init__(self, job_id: str, status: str = "Running") -> None:
        self.id = job_id
        self.name = "n"
        self.job_details_page = f"https://app.example.test/job/{job_id}"
        self.status = status
        self.preliminary: list[bool] = []

    def get_status(self) -> str:
        return self.status

    def get_progress(self) -> SimpleNamespace:
        return SimpleNamespace(state=self.status, completion_percentage=40.04)

    def pause(self) -> "FakeJob":
        self.status = "Paused"
        return self

    def resume(self) -> "FakeJob":
        self.status = "Running"
        return self

    def get_results(self, preliminary_results: bool = False) -> dict[str, Any]:
        self.preliminary.append(preliminary_results)
        return {"info": {}, "summary": {}, "results": [{"a": 1}]}


class FakeAudience:
    def __init__(self, audience_id: str, name: str, status: str = "Ready") -> None:
        self.id = audience_id
        self.name = name
        self.status = status
        self.funnel = SimpleNamespace(graduated=0, distilling=0, dropped=0, inactive=0)
        self.filtered_with: list[Any] = []
        self.assigned: list[Any] = []

    def get_recruiting_metrics(self) -> SimpleNamespace:
        return self.funnel

    def filter(self, filters: list[Any]) -> "FakeAudience":
        self.filtered_with = filters
        narrowed = FakeAudience(f"fau_{self.id}", self.name)
        narrowed.assigned = self.assigned
        return narrowed

    def assign_job(self, definition: Any) -> FakeJob:
        self.assigned.append(definition.id)
        return FakeJob("job_1")


class NotFound(Exception):
    status_code = 404


class FakeAudiences:
    def __init__(self) -> None:
        self.known = {
            "global": FakeAudience("global", "Global"),
            "aud_new": FakeAudience("aud_new", "Dentists", status="Created"),
        }

    def get_audience_by_id(self, audience_id: str) -> FakeAudience:
        if audience_id not in self.known:
            raise NotFound("not found")
        return self.known[audience_id]

    def audience_audience_id_get(self, audience_id: str) -> SimpleNamespace:
        from rapidata.api_client.models.audience_status import AudienceStatus

        return SimpleNamespace(status=AudienceStatus(self.known[audience_id].status))


class FakeBilling:
    def __init__(self) -> None:
        self.summary = SimpleNamespace(
            mode=SimpleNamespace(value="Prepaid"),
            current_period_usage=0.3,
            effective_limit=20.0,
            available_amount=19.7,
        )
        self.owed: float | Exception = 0.0

    def billing_summary_get(self) -> SimpleNamespace:
        return self.summary

    def get_outstanding_balance(self) -> float:
        if isinstance(self.owed, Exception):
            raise self.owed
        return self.owed


class FakeProvider:
    def __init__(self) -> None:
        billing = FakeBilling()
        audiences = FakeAudiences()
        self.client = SimpleNamespace(
            asset=FakeAssets(),
            job=FakeJobs(),
            billing=billing,
            audience=audiences,
            _openapi_service=SimpleNamespace(
                payment=SimpleNamespace(billing_api=billing),
                audience=SimpleNamespace(audience_api=audiences),
            ),
        )

    def get_client(self) -> SimpleNamespace:
        return self.client

    def get_api_token(self) -> str:
        return "api-token"


class Api:
    """Records the requests the tools send to the upload-session endpoints."""

    def __init__(self, responses: dict[str, httpx.Response]) -> None:
        self.responses = responses
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses[f"{request.method} {request.url.path}"]


def _server(api: Api | None = None) -> tuple[FastMCP, FakeProvider]:
    provider = FakeProvider()
    mcp = FastMCP("test")
    register_tools(
        mcp,
        lambda: provider,
        api_url=API,
        public_url=f"{PUBLIC}/",
        http=httpx.Client(transport=httpx.MockTransport(api or Api({}))),
    )
    return mcp, provider


def _call(mcp: FastMCP, tool: str, **arguments: Any) -> dict[str, Any]:
    result = asyncio.run(mcp.call_tool(tool, arguments))
    assert isinstance(result, tuple), result
    return result[1]


def test_upload_media_declares_chatgpt_file_params():
    mcp, _ = _server()
    tool = next(t for t in asyncio.run(mcp.list_tools()) if t.name == "upload_media")

    assert tool.meta == {"openai/fileParams": ["files"]}
    files = tool.inputSchema["properties"]["files"]
    assert files["type"] == "array"
    item = tool.inputSchema["$defs"][files["items"]["$ref"].rsplit("/", 1)[-1]]
    assert set(item["properties"]) == {
        "download_url",
        "file_id",
        "mime_type",
        "file_name",
    }
    assert sorted(item["required"]) == ["download_url", "file_id"]
    assert "files" not in tool.inputSchema.get("required", [])


def test_upload_media_without_inputs_opens_an_upload_session():
    api = Api(
        {
            "POST /asset/upload-session": httpx.Response(
                200,
                json={
                    "id": "ups_1",
                    "uploadToken": "s3cr/t+1",
                    "expiresAt": "2026-11-03T12:00:00Z",
                    "maxFiles": 100,
                    "maxFileSizeBytes": 52428800,
                },
            )
        }
    )
    mcp, provider = _server(api)

    result = _call(mcp, "upload_media")

    assert api.requests[0].headers["authorization"] == "Bearer api-token"
    assert result["assets"] == []
    assert result["upload_session_id"] == "ups_1"
    assert result["upload_page_url"] == f"{PUBLIC}/upload/ups_1#t=s3cr%2Ft%2B1"
    assert result["curl_command"] == (
        "curl -sS -H 'X-Upload-Token: s3cr/t+1' -F 'file=@<path>' "
        f"'{API}/asset/upload-session/ups_1/file'"
    )
    assert result["expires_at"] == "2026-11-03T12:00:00Z"
    assert "run curl_command once per file" in result["next_step"]
    assert "list_uploads" in result["next_step"]
    assert provider.client.asset.imported == []


def test_upload_media_imports_urls_and_chatgpt_files():
    mcp, provider = _server()

    result = _call(
        mcp,
        "upload_media",
        urls=["https://example.test/a.png"],
        files=[
            {
                "download_url": "https://files.example.test/1",
                "file_id": "file_1",
                "file_name": "cat.png",
                "mime_type": "image/png",
            },
            {"download_url": "https://files.example.test/2", "file_id": "file_2"},
        ],
    )

    assert [(a["source"], a["reference"], a["error"]) for a in result["assets"]] == [
        ("https://example.test/a.png", "ref-of-a.png", None),
        ("cat.png", "ref-of-1", None),
        ("file_2", "ref-of-2", None),
    ]
    assert sorted(provider.client.asset.imported) == [
        "https://example.test/a.png",
        "https://files.example.test/1",
        "https://files.example.test/2",
    ]
    assert result.get("upload_session_id") is None


def test_upload_media_reports_a_failed_import_on_its_item():
    mcp, _ = _server()

    result = _call(
        mcp, "upload_media", urls=["https://example.test/a.png", "https://broken.test/b"]
    )

    assert result["assets"][1] == {
        "source": "https://broken.test/b",
        "reference": None,
        "error": "could not download",
    }
    assert result["next_step"].startswith("1 of 2 could not be imported")


@pytest.mark.parametrize("url", ["/var/run/secrets/token", "file:///etc/hosts", "a.b"])
def test_upload_media_never_reads_a_server_path(url):
    mcp, provider = _server()

    with pytest.raises(ToolError, match=r"not an http\(s\) URL.*upload_media\(\)"):
        _call(mcp, "upload_media", urls=[url])
    assert provider.client.asset.imported == []


def test_list_uploads_returns_references():
    api = Api(
        {
            "GET /asset/upload-session/ups_1/files": httpx.Response(
                200,
                json={
                    "items": [
                        {"originalName": "a.png", "fileName": REF_A, "uploadedAt": "x"},
                        {"originalName": "b.mp4", "fileName": REF_B, "uploadedAt": "y"},
                    ],
                    "total": 2,
                    "page": 1,
                    "pageSize": 100,
                    "totalPages": 1,
                },
            )
        }
    )
    mcp, _ = _server(api)

    result = _call(mcp, "list_uploads", upload_session_id="ups_1")

    request = api.requests[0]
    assert request.headers["authorization"] == "Bearer api-token"
    assert request.url.params["page_size"] == "100"
    assert result["upload_session_id"] == "ups_1"
    assert result["assets"] == [
        {"source": "a.png", "reference": REF_A, "error": None},
        {"source": "b.mp4", "reference": REF_B, "error": None},
    ]


def test_list_uploads_of_an_empty_session_says_to_wait():
    empty = {"items": [], "total": 0, "page": 1, "pageSize": 100, "totalPages": 0}
    route = "GET /asset/upload-session/ups_1/files"
    api = Api({route: httpx.Response(200, json=empty)})
    mcp, _ = _server(api)

    result = _call(mcp, "list_uploads", upload_session_id="ups_1")

    assert result["assets"] == []
    assert result["next_step"].startswith("Nothing has been uploaded yet.")


@pytest.mark.parametrize(
    ("status", "message"),
    [(403, "'ups_x' belongs to another Rapidata account"), (404, "'ups_x' not found")],
)
def test_list_uploads_explains_a_refused_session(status, message):
    api = Api({"GET /asset/upload-session/ups_x/files": httpx.Response(status)})
    mcp, _ = _server(api)

    with pytest.raises(ToolError, match=message):
        _call(mcp, "list_uploads", upload_session_id="ups_x")


def test_create_classification_task_passes_uploaded_assets():
    import rapidata

    mcp, provider = _server()

    result = _call(
        mcp,
        "create_classification_task",
        name="n",
        instruction="i",
        answer_options=["yes", "no"],
        datapoint_references=[REF_A, f" {REF_B} "],
        responses_per_datapoint=3,
    )

    datapoints = provider.client.job.created[0]["datapoints"]
    assert all(isinstance(d, rapidata.UploadedAsset) for d in datapoints)
    assert [d.name for d in datapoints] == [REF_A, REF_B]
    assert result["total_responses"] == 6


def test_create_classification_task_without_references_is_text_only():
    from rapidata_mcp import tools

    mcp, provider = _server()

    _call(
        mcp,
        "create_classification_task",
        name="n",
        instruction="Is 7 prime?",
        answer_options=["yes", "no"],
    )

    created = provider.client.job.created[0]
    assert created["datapoints"] == [tools._PLACEHOLDER_IMAGE_URL]


def test_create_comparison_task_passes_uploaded_assets():
    import rapidata

    mcp, provider = _server()

    _call(
        mcp,
        "create_comparison_task",
        name="n",
        instruction="Which is better?",
        comparison_pairs=[[REF_A, REF_B]],
    )

    pairs = provider.client.job.created[0]["datapoints"]
    assert all(isinstance(d, rapidata.UploadedAsset) for d in pairs[0])
    assert [d.name for d in pairs[0]] == [REF_A, REF_B]


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("https://example.test/a.png", r"is a URL .*upload_media\(urls="),
        ("/Users/me/cat.png", r"not an asset reference.*upload_media\(\)"),
        ("C:\\Users\\me\\cat.png", r"not an asset reference.*upload_media\(\)"),
        ("cat.png", r"not an asset reference.*upload_media\(\)"),
    ],
)
def test_create_tools_reject_anything_but_a_reference(value, message):
    mcp, provider = _server()

    with pytest.raises(ToolError, match=rf"datapoint_references\[1\] .*{message}"):
        _call(
            mcp,
            "create_classification_task",
            name="n",
            instruction="i",
            answer_options=["yes", "no"],
            datapoint_references=[REF_A, value],
        )
    with pytest.raises(ToolError, match=rf"comparison_pairs\[0\]\[0\] .*{message}"):
        _call(
            mcp,
            "create_comparison_task",
            name="n",
            instruction="i",
            comparison_pairs=[[value, REF_A]],
        )
    assert provider.client.job.created == []


def test_an_argument_from_an_older_tool_list_is_rejected_not_dropped():
    mcp, provider = _server()

    with pytest.raises(ToolError, match=r"takes no datapoint_urls\. .*outdated"):
        _call(
            mcp,
            "create_classification_task",
            name="n",
            instruction="Is this a cat?",
            answer_options=["yes", "no"],
            datapoint_urls=["https://example.test/cat.png"],
        )
    with pytest.raises(ToolError, match=r"Unknown tool: create_task\. .*outdated"):
        _call(mcp, "create_task", name="n")
    assert provider.client.job.created == []


def test_a_url_where_a_reference_belongs_mentions_an_outdated_tool_list():
    mcp, _ = _server()

    with pytest.raises(ToolError, match=r"no upload_media tool.*outdated tool list"):
        _call(
            mcp,
            "create_comparison_task",
            name="n",
            instruction="i",
            comparison_pairs=[["https://example.test/a.png", REF_A]],
        )


def _create_comparison(mcp: FastMCP) -> dict[str, Any]:
    return _call(
        mcp,
        "create_comparison_task",
        name="voice-overs",
        instruction="Which voice-over sounds more natural?",
        comparison_pairs=[[REF_A, REF_B]],
        responses_per_datapoint=20,
    )


def test_a_draft_shows_its_price_and_the_credit_left():
    mcp, _ = _server()

    result = _create_comparison(mcp)

    assert result["total_responses"] == 20
    assert result["estimated_cost_usd"] == 0.3
    assert result["budget_left_usd"] == 19.7
    assert "for about $0.30" in result["next_step"]
    assert "$19.70 left" in result["next_step"]


def test_a_draft_comes_back_without_a_price_after_the_limit(monkeypatch):
    monkeypatch.setattr(tools, "_PRICE_TIMEOUT_S", 0.2)
    monkeypatch.setattr(FakeDefinition, "pricing_delay", 1.0)
    mcp, _ = _server()

    started = time.monotonic()
    result = _create_comparison(mcp)

    assert time.monotonic() - started < 0.9
    assert result["estimated_cost_usd"] is None
    assert result["budget_left_usd"] == 19.7
    assert "about $" not in result["next_step"]


def test_account_balance_for_a_new_prepaid_account():
    mcp, _ = _server()

    assert _call(mcp, "get_account_balance") == {
        "mode": "prepaid",
        "budget_left_usd": 19.7,
        "spent_this_period_usd": 0.3,
        "spend_limit_usd": 20.0,
        "owed_usd": 0.0,
    }


def test_account_balance_under_a_spending_cap():
    mcp, provider = _server()
    provider.client.billing.summary = SimpleNamespace(
        mode=SimpleNamespace(value="Limited"),
        current_period_usage=120.456,
        effective_limit=100.0,
        available_amount=None,
    )
    provider.client.billing.owed = RuntimeError("no invoices yet")

    result = _call(mcp, "get_account_balance")

    assert result["mode"] == "limited"
    assert result["budget_left_usd"] == 0.0
    assert result["spent_this_period_usd"] == 120.46
    assert result["owed_usd"] is None


def test_start_job_runs_on_the_global_audience_by_default():
    mcp, provider = _server()

    result = _call(mcp, "start_job", job_definition_id="jd_1")

    global_audience = provider.client.audience.known["global"]
    assert global_audience.assigned == ["jd_1"]
    assert global_audience.filtered_with == []
    assert (result["audience_id"], result["audience"]) == ("global", "Global")


def test_start_job_filters_the_audience_by_language():
    from rapidata import LanguageFilter

    mcp, provider = _server()

    result = _call(
        mcp, "start_job", job_definition_id="jd_1", filters={"languages": ["DE"]}
    )

    global_audience = provider.client.audience.known["global"]
    assert global_audience.filtered_with == [LanguageFilter(["de"])]
    assert global_audience.assigned == ["jd_1"]
    assert result["audience_id"] == "fau_global"
    assert result["audience"] == "Global, filtered to languages de"


def test_the_filters_description_says_when_to_filter():
    mcp, _ = _server()
    tool = next(t for t in asyncio.run(mcp.list_tools()) if t.name == "start_job")

    description = tool.inputSchema["properties"]["filters"]["description"]
    assert "speech in audio or video" in description
    assert "machine-translated" in description


@pytest.mark.parametrize(
    "arguments, message",
    [
        ({"filters": {"countries": ["Germany"]}}, "two-letter codes"),
        ({"audience_id": "aud_missing"}, "Unknown audience 'aud_missing'"),
        ({"audience_id": "aud_new"}, "'Dentists' .* can never answer"),
    ],
)
def test_start_job_refuses(arguments, message):
    mcp, provider = _server()

    with pytest.raises(ToolError, match=message):
        _call(mcp, "start_job", job_definition_id="jd_1", **arguments)
    assert all(not a.assigned for a in provider.client.audience.known.values())


def test_a_recruiting_custom_audience_is_not_refused():
    mcp, provider = _server()
    audience = provider.client.audience.known["aud_new"]
    audience.funnel.distilling = 3

    _call(mcp, "start_job", job_definition_id="jd_1", audience_id="aud_new")

    assert audience.assigned == ["jd_1"]


def test_job_status_reports_the_share_done():
    mcp, provider = _server()
    provider.client.job.jobs["job_1"] = FakeJob("job_1")

    result = _call(mcp, "get_job_status", job_id="job_1")

    assert (result["status"], result["completion_percentage"]) == ("Running", 40.0)


def test_pause_then_resume():
    mcp, provider = _server()
    job = provider.client.job.jobs["job_1"] = FakeJob("job_1")

    assert _call(mcp, "pause_job", job_id="job_1")["status"] == "Paused"
    assert _call(mcp, "resume_job", job_id="job_1") == {
        "job_id": "job_1",
        "status": "Running",
        "paused": False,
    }
    assert job.status == "Running"
    with pytest.raises(ToolError, match="is Running, not Paused"):
        _call(mcp, "resume_job", job_id="job_1")


def test_a_paused_job_returns_preliminary_results():
    mcp, provider = _server()
    job = provider.client.job.jobs["job_1"] = FakeJob("job_1", status="Paused")

    result = _call(mcp, "get_job_results", job_id="job_1")

    assert job.preliminary == [True]
    assert result["result_status"] == "partial"
    assert result["results"] == [{"a": 1}]


def test_keep_option_order_turns_shuffling_off():
    from rapidata import NoShuffleSetting

    mcp, provider = _server()

    _call(
        mcp,
        "create_classification_task",
        name="n",
        instruction="How sharp is this image?",
        answer_options=["1", "2", "3", "4", "5"],
        datapoint_references=[REF_A],
        keep_option_order=True,
    )
    _call(
        mcp,
        "create_classification_task",
        name="n",
        instruction="Is this a cat?",
        answer_options=["yes", "no"],
        datapoint_references=[REF_A],
    )

    first, second = provider.client.job.created
    assert [type(s) for s in first["settings"]] == [NoShuffleSetting]
    assert second["settings"] is None
