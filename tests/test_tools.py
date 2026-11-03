import asyncio
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

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


class FakeJobs:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    def _create(self, **kwargs: Any) -> SimpleNamespace:
        self.created.append(kwargs)
        return SimpleNamespace(id="jd_1", name=kwargs["name"], _job_details_page="u")

    create_classification_job_definition = _create
    create_compare_job_definition = _create


class FakeProvider:
    def __init__(self) -> None:
        self.client = SimpleNamespace(asset=FakeAssets(), job=FakeJobs())

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
    assert "curl_command" in result["next_step"]
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
            "GET /asset/upload-session/ups_1": httpx.Response(
                200,
                json={
                    "id": "ups_1",
                    "expiresAt": "2026-11-03T12:00:00Z",
                    "files": [
                        {"originalName": "a.png", "fileName": REF_A, "uploadedAt": "x"},
                        {"originalName": "b.mp4", "fileName": REF_B, "uploadedAt": "y"},
                    ],
                },
            )
        }
    )
    mcp, _ = _server(api)

    result = _call(mcp, "list_uploads", upload_session_id="ups_1")

    assert api.requests[0].headers["authorization"] == "Bearer api-token"
    assert result["assets"] == [
        {"source": "a.png", "reference": REF_A, "error": None},
        {"source": "b.mp4", "reference": REF_B, "error": None},
    ]
    assert result["expires_at"] == "2026-11-03T12:00:00Z"


def test_list_uploads_of_an_unknown_session_is_a_clear_error():
    api = Api({"GET /asset/upload-session/ups_x": httpx.Response(404)})
    mcp, _ = _server(api)

    with pytest.raises(ToolError, match="'ups_x' not found"):
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
