"""The Rapidata MCP tool layer.

Exposes a small, curated set of tools that let an MCP-capable agent run human
labeling tasks on Rapidata: upload media as asset references, create a
classification or comparison job definition from them (a draft, no spend), start
it on an audience (the single money-spending step), follow, pause and resume it,
and fetch shaped results.

The create step never spends: it produces a *draft job definition* and asks the
caller to confirm with the user before ``start_job`` runs it on an audience.
This mirrors the platform's own split between a job definition (the
template) and a job (a run of that definition against an audience).

Tools resolve their client through a ``provider_factory`` rather than holding a
fixed client, so the hosted transport can hand each call a per-request client
scoped to the authenticated customer (see :mod:`rapidata_mcp.server`). Tool
behaviour is otherwise identical regardless of how the client is resolved.
"""

from __future__ import annotations

import contextvars
import logging
import os
import time
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import TYPE_CHECKING, Annotated, Any, Literal
from urllib.parse import quote

import httpx
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BeforeValidator, Field
from typing_extensions import NotRequired, TypedDict

from rapidata_mcp.auth import ClientProvider
from rapidata_mcp.results import summarize_results
from rapidata_mcp.uploads import UploadSessions, check_reference, is_http_url

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP
    from rapidata import RapidataClient, UploadedAsset
    from rapidata.rapidata_client.job.rapidata_job import RapidataJob
    from rapidata.rapidata_client.job.rapidata_job_definition import (
        RapidataJobDefinition,
    )

logger = logging.getLogger(__name__)

# The well-known id of the global audience — the broadest pool of labelers,
# ready to work immediately with no targeting or qualification setup.
_GLOBAL_AUDIENCE_ID = "global"

FILTERS_RULE = (
    "Filter by language only when people must understand speech in audio or video, "
    "or text inside an image or video: the question, context and answer options are "
    "already machine-translated into each person's language. Filter by country only "
    "when the user wants the opinion of people in that country."
)

# Datapoint used when the caller creates a task without any media: a task still
# needs at least one datapoint, so a generic question-mark image stands in and
# the crowd answers the instruction on its own. Overridable so the placeholder
# can later point at a Rapidata-hosted asset.
_PLACEHOLDER_IMAGE_URL = os.environ.get(
    "RAPIDATA_MCP_PLACEHOLDER_IMAGE_URL",
    "https://placehold.co/600x600.png?text=%3F",
)

# Job states in which no result file exists yet, mapped to the result_status an
# agent should poll on so get_job_results never blocks on a running job.
_PENDING_STATES: dict[str, str] = {
    "Submitted": "not_started",
    "Queued": "not_started",
    "Running": "collecting",
    "ManualApproval": "manual_review",
    "StaleResults": "regenerating",
    "SpendLimited": "spend_limited",
}

# Every tool reaches the Rapidata platform (an external service), the global
# crowd of human annotators, and caller-supplied public media URLs — an open
# world of external entities, so openWorldHint is true throughout.
_OPEN_WORLD = True

_MAX_IMPORTS = 100
_IMPORT_WORKERS = 8

# The SDK prices a draft within about 5 s but polls for up to 5 minutes; a draft
# waits at most this long and comes back without a price after it.
_PRICE_TIMEOUT_S = 10.0
_pricing = ThreadPoolExecutor(max_workers=32, thread_name_prefix="pricing")

_OUTDATED_TOOLS = (
    "Your client is showing an outdated list of Rapidata tools. Ask the user to start "
    "a new chat or refresh the Rapidata connector, then use the tools it lists."
)

_USE_REFERENCES = (
    "Pass the references to create_classification_task (datapoint_references) or "
    "create_comparison_task (comparison_pairs); they take nothing else."
)


# ChatGPT's file object; its app review rejects a schema that drops one of the four
# properties or requires mime_type or file_name.
class ChatGPTFile(TypedDict):
    download_url: str
    file_id: str
    mime_type: NotRequired[str]
    file_name: NotRequired[str]


class AssetReference(TypedDict):
    source: str
    reference: str | None
    error: str | None


class _MediaUploadBase(TypedDict):
    assets: list[AssetReference]
    next_step: str


# FastMCP serialises absent keys as null, so every optional field must allow it.
class MediaUpload(_MediaUploadBase, total=False):
    upload_session_id: str | None
    upload_page_url: str | None
    curl_command: str | None
    expires_at: str | None


class UploadList(TypedDict):
    upload_session_id: str
    assets: list[AssetReference]
    next_step: str


class DraftResult(TypedDict):
    job_definition_id: str
    name: str
    status: str
    details_url: str
    runs_on: str
    total_responses: int
    estimated_cost_usd: float | None
    budget_left_usd: float | None
    confirmation_required: bool
    next_step: str


class AccountBalance(TypedDict):
    mode: Literal["prepaid", "limited", "usage"]
    budget_left_usd: float | None
    spent_this_period_usd: float
    spend_limit_usd: float | None
    owed_usd: float | None


class Filters(TypedDict, total=False):
    countries: list[str]
    languages: list[str]


class StartedJob(TypedDict):
    job_id: str
    name: str
    status: str
    details_url: str
    audience_id: str
    audience: str
    started: bool


class JobStatus(TypedDict):
    job_id: str
    status: str
    completion_percentage: float
    details_url: str


class _JobResultsBase(TypedDict):
    job_id: str
    status: str
    result_status: str
    results: list[dict[str, Any]] | None


# FastMCP serialises absent keys as null, so every optional field must allow it.
class JobResults(_JobResultsBase, total=False):
    error: str | None
    detail: str | None
    info: dict[str, Any] | None
    summary: dict[str, Any] | None
    datapoint_count: int | None
    returned_datapoints: int | None
    truncated: bool | None


class JobSummary(TypedDict):
    job_id: str
    name: str
    status: str
    details_url: str


class JobList(TypedDict):
    jobs: list[JobSummary]


class PausedJob(TypedDict):
    job_id: str
    status: str
    paused: bool


def _none_as_empty(value: Any) -> Any:
    return () if value is None else value


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def register_tools(
    mcp: FastMCP,
    provider_factory: Callable[[], ClientProvider],
    *,
    api_url: str = "https://api.rapidata.ai",
    public_url: str = "https://mcp.rapidata.ai",
    http: httpx.Client | None = None,
) -> None:
    """Register all Rapidata tools on ``mcp``.

    Args:
        mcp: The FastMCP server to register tools on.
        provider_factory: Resolves the :class:`ClientProvider` for the current
            call. Called once per tool invocation so per-request, token-scoped
            clients work without the tools knowing about the auth model.
        api_url: Base URL of the Rapidata API the upload sessions live on.
        public_url: This server's public origin, which serves the upload page.
        http: HTTP client for the upload-session calls.
    """
    sessions = UploadSessions(api_url, http)
    public_url = public_url.rstrip("/")

    def _client():
        return provider_factory().get_client()

    def _uploaded(reference: str, where: str) -> UploadedAsset:
        from rapidata import UploadedAsset

        return UploadedAsset(name=check_reference(reference, where))

    def _job(job_id: str) -> RapidataJob:
        return _client().job.get_job_by_id(job_id)

    def _definition(job_definition_id: str) -> RapidataJobDefinition:
        return _client().job.get_job_definition_by_id(job_definition_id)

    def _draft_result(
        client: RapidataClient,
        definition: RapidataJobDefinition,
        total_responses: int,
    ) -> DraftResult:
        price = _pricing.submit(
            contextvars.copy_context().run, _estimated_cost, definition
        )
        balance = _pricing.submit(
            contextvars.copy_context().run, _account_balance, client
        )
        deadline = time.monotonic() + _PRICE_TIMEOUT_S
        cost = _within(price, deadline)
        account = _within(balance, deadline)
        budget_left = account["budget_left_usd"] if account else None

        cost_text = f"{total_responses} human responses"
        if cost is not None:
            cost_text += f" for about ${cost:.2f} (Rapidata's estimate)"
        credit_text = ""
        if budget_left is not None:
            credit_text = f" The account has ${budget_left:.2f} left to spend."
        return {
            "job_definition_id": definition.id,
            "name": definition.name,
            "status": "draft",
            # Preview page for the definition before it runs.
            "details_url": definition._job_details_page,
            "runs_on": "global audience unless start_job names another",
            "total_responses": total_responses,
            "estimated_cost_usd": cost,
            "budget_left_usd": budget_left,
            "confirmation_required": True,
            "next_step": (
                "This job definition is a DRAFT and is NOT collecting responses "
                f"yet. Starting it will collect {cost_text} and spend money."
                f"{credit_text} Present this cost to the user and ask whether to "
                "start collecting. Only after the user confirms, call start_job "
                "with this job_definition_id."
            ),
        }

    def _open_upload_session() -> MediaUpload:
        session = sessions.create(provider_factory().get_api_token())
        session_id: str = session["id"]
        upload_token: str = session["uploadToken"]
        max_files = session.get("maxFiles") or 100
        max_mb = (session.get("maxFileSizeBytes") or 50 * 1024 * 1024) // (1024 * 1024)
        # The token rides in the fragment, which browsers never send to a server.
        page_url = (
            f"{public_url}/upload/{quote(session_id, safe='')}"
            f"#t={quote(upload_token, safe='')}"
        )
        curl_command = " ".join(
            [
                "curl -sS -H",
                _shell_quote(f"X-Upload-Token: {upload_token}"),
                "-F",
                _shell_quote("file=@<path>"),
                _shell_quote(sessions.file_endpoint(session_id)),
            ]
        )
        return {
            "assets": [],
            "upload_session_id": session_id,
            "upload_page_url": page_url,
            "curl_command": curl_command,
            "expires_at": session.get("expiresAt"),
            "next_step": (
                "This is an upload link for local files. If you can run shell "
                "commands on the machine that has the files, run curl_command once "
                "per file with <path> replaced by that file's path. Never put "
                "several files in one request: the API rejects a request over 100 "
                "MB, and one bad file fails the whole request. Each run prints JSON "
                "whose fileName is that file's reference, or an error saying why the "
                "file was rejected; tell the user about it. If you cannot run curl "
                "(or it cannot reach the API), show the user upload_page_url as a "
                "link, ask them to drop the files there and to tell you when they "
                "are done, then call list_uploads with this upload_session_id. The "
                f"link takes up to {max_files} files of at most {max_mb} MB each "
                f"until expires_at. {_USE_REFERENCES}"
            ),
        }

    @mcp.tool(
        title="Upload media",
        annotations=ToolAnnotations(
            title="Upload media",
            # Stores media or opens an upload link: no spend, nothing destroyed.
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=_OPEN_WORLD,
        ),
        # ChatGPT fills `files` with the files the user attached to the chat.
        meta={"openai/fileParams": ["files"]},
    )
    def upload_media(
        urls: Annotated[
            list[str] | None,
            Field(
                description=(
                    "Public http(s) URLs of images, videos or audio files to import. "
                    "One reference comes back per URL."
                )
            ),
        ] = None,
        files: Annotated[
            Sequence[ChatGPTFile],
            BeforeValidator(_none_as_empty),
            Field(
                description=(
                    "Files the user attached in ChatGPT; ChatGPT fills these in. "
                    "One reference comes back per file."
                )
            ),
        ] = (),
    ) -> MediaUpload:
        """Turn media into asset references, the only media the create tools accept.

        Media already online: pass its public URLs as ``urls``. Files the user
        attached in ChatGPT arrive as ``files``. Both are imported right away and
        return one reference each, or an ``error`` for an item that failed.

        Local files (on the user's computer or yours): call this with neither. It
        opens a one-hour upload link and returns a ready-to-run ``curl_command``
        for agents with a shell, and an ``upload_page_url`` the user can drop files
        on, after which ``list_uploads`` returns their references. Follow
        ``next_step``. Uploading never spends; it does not create or start a task.
        """
        sources: list[tuple[str, str]] = []
        for i, url in enumerate(urls or []):
            url = url.strip()
            if not is_http_url(url):
                raise ValueError(
                    f"urls[{i}] is not an http(s) URL. For a local file, call "
                    "upload_media() without arguments and follow its next_step."
                )
            sources.append((url, url))
        for i, file in enumerate(files):
            if not is_http_url(file["download_url"]):
                raise ValueError(f"files[{i}].download_url is not an http(s) URL")
            source = file.get("file_name") or file["file_id"]
            sources.append((source, file["download_url"]))

        if not sources:
            return _open_upload_session()
        if len(sources) > _MAX_IMPORTS:
            raise ValueError(
                f"upload_media imports at most {_MAX_IMPORTS} items per call; "
                "split them across several calls."
            )

        client = _client()

        def import_one(item: tuple[str, str]) -> AssetReference:
            source, url = item
            try:
                reference = client.asset.upload(url).name
            except Exception as e:
                error = str(e) or type(e).__name__
                return {"source": source, "reference": None, "error": error[:500]}
            return {"source": source, "reference": reference, "error": None}

        workers = min(len(sources), _IMPORT_WORKERS)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            # A context per import keeps its API calls inside this tool call's trace.
            futures = [
                pool.submit(contextvars.copy_context().run, import_one, source)
                for source in sources
            ]
            assets = [future.result() for future in futures]

        failed = sum(1 for asset in assets if asset["error"] is not None)
        next_step = _USE_REFERENCES
        if failed:
            next_step = (
                f"{failed} of {len(assets)} could not be imported (see error). "
                "Each URL must be public and point straight at an image, video or "
                f"audio file. {_USE_REFERENCES}"
            )
        return {"assets": assets, "next_step": next_step}

    @mcp.tool(
        title="List uploads",
        annotations=ToolAnnotations(
            title="List uploads",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=_OPEN_WORLD,
        ),
    )
    def list_uploads(
        upload_session_id: Annotated[
            str,
            Field(description="The upload_session_id that upload_media returned."),
        ],
    ) -> UploadList:
        """List the files uploaded to an upload_media link, with their references.

        Call it once the user says they finished dropping files on the upload page
        (curl already prints the references). Returns every file uploaded so far,
        named as the user's original file; the list stays readable for 7 days.
        """
        session_id = upload_session_id.strip()
        files = sessions.files(provider_factory().get_api_token(), session_id)
        assets: list[AssetReference] = [
            {
                "source": file.get("originalName") or file["fileName"],
                "reference": file["fileName"],
                "error": None,
            }
            for file in files
        ]
        next_step = _USE_REFERENCES
        if not assets:
            next_step = (
                "Nothing has been uploaded yet. Ask the user to drop their files on "
                "the upload page and to tell you when they are done, then call "
                "list_uploads again. The link takes uploads until the expires_at "
                "that upload_media returned; after that, call upload_media() for a "
                "new one."
            )
        return {
            "upload_session_id": session_id,
            "assets": assets,
            "next_step": next_step,
        }

    @mcp.tool(
        title="Create classification task",
        annotations=ToolAnnotations(
            title="Create classification task",
            # Creates a reversible draft job definition; no spend, nothing destroyed.
            # Each call creates a new definition, so repeats are not idempotent.
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=_OPEN_WORLD,
        ),
    )
    def create_classification_task(
        name: Annotated[
            str,
            Field(description="Internal label for the task; not shown to annotators."),
        ],
        instruction: Annotated[
            str, Field(description="The question annotators answer for each item.")
        ],
        answer_options: Annotated[
            list[str],
            Field(
                min_length=2,
                description=(
                    "The answers annotators pick between, shown verbatim; results "
                    "are tallied per option text."
                ),
            ),
        ],
        datapoint_references: Annotated[
            list[str] | None,
            Field(
                description=(
                    "One asset reference per item to label (image, video or audio), "
                    "as returned by upload_media or list_uploads. URLs and file paths "
                    "are rejected: run them through upload_media first. Omit only "
                    "for a genuinely text-only question with no media at all; "
                    "annotators then see a generic placeholder image."
                )
            ),
        ] = None,
        responses_per_datapoint: Annotated[
            int, Field(description="Number of human responses to collect per item.")
        ] = 10,
        contexts: Annotated[
            list[str] | None,
            Field(
                description=(
                    "Optional per-datapoint text shown alongside the instruction. "
                    "If given, must have one entry per datapoint."
                )
            ),
        ] = None,
        confidence_threshold: Annotated[
            float | None,
            Field(
                description=(
                    "Optional early stop, as a probability strictly between 0 and 1: "
                    "stop collecting for a datapoint once the leading answer is this "
                    "likely to be the crowd's true choice, or when "
                    "responses_per_datapoint is reached. Scales logarithmically "
                    "(0.99 to 0.999 is a big step). Omit to always collect "
                    "responses_per_datapoint."
                )
            ),
        ] = None,
        keep_option_order: Annotated[
            bool,
            Field(
                description=(
                    "Show answer_options in the given order instead of shuffling "
                    "them per person. Use it for scales (1 to 5, bad to good)."
                )
            ),
        ] = False,
    ) -> DraftResult:
        """Create a classification task where humans pick one answer option per item.

        Media goes in as asset references from ``upload_media``, never as URLs or
        paths. Leave ``datapoint_references`` out only for a text-only question:
        never as a fallback for media you could not upload.

        Creates a DRAFT job definition and does NOT start collecting responses (or
        spend): it returns ``confirmation_required`` and expects you to confirm the
        cost with the user, then call ``start_job``, which picks who answers.
        """
        datapoints: list[str | UploadedAsset] = (
            [
                _uploaded(reference, f"datapoint_references[{i}]")
                for i, reference in enumerate(datapoint_references)
            ]
            if datapoint_references
            else [_PLACEHOLDER_IMAGE_URL]
        )
        if contexts is not None and len(contexts) != len(datapoints):
            raise ValueError(
                "contexts, when provided, must have one entry per datapoint "
                "(same length as datapoint_references)"
            )

        from rapidata import NoShuffleSetting

        client = _client()
        definition = client.job.create_classification_job_definition(
            name=name,
            instruction=instruction,
            answer_options=answer_options,
            datapoints=datapoints,
            responses_per_datapoint=responses_per_datapoint,
            contexts=contexts,
            confidence_threshold=confidence_threshold,
            settings=[NoShuffleSetting()] if keep_option_order else None,
        )
        return _draft_result(
            client, definition, len(datapoints) * responses_per_datapoint
        )

    @mcp.tool(
        title="Create comparison task",
        annotations=ToolAnnotations(
            title="Create comparison task",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=_OPEN_WORLD,
        ),
    )
    def create_comparison_task(
        name: Annotated[
            str,
            Field(description="Internal label for the task; not shown to annotators."),
        ],
        instruction: Annotated[
            str, Field(description="The question annotators answer for each pair.")
        ],
        comparison_pairs: Annotated[
            list[list[str]],
            Field(
                description=(
                    "List of [reference_a, reference_b] pairs; each pair holds "
                    "exactly two asset references, as returned by upload_media or "
                    "list_uploads. URLs and file paths are rejected: run them "
                    "through upload_media first."
                )
            ),
        ],
        responses_per_datapoint: Annotated[
            int, Field(description="Number of human responses to collect per pair.")
        ] = 10,
        contexts: Annotated[
            list[str] | None,
            Field(
                description=(
                    "Optional per-pair text shown alongside the instruction. If "
                    "given, must have one entry per pair."
                )
            ),
        ] = None,
        confidence_threshold: Annotated[
            float | None,
            Field(
                description=(
                    "Optional early stop, as a probability strictly between 0 and "
                    "1: stop collecting for a pair once the leading item is this "
                    "likely to be the crowd's true preference, or when "
                    "responses_per_datapoint is reached. Scales logarithmically "
                    "(0.99 to 0.999 is a big step). Omit to always collect "
                    "responses_per_datapoint."
                )
            ),
        ] = None,
    ) -> DraftResult:
        """Create a pairwise comparison task: humans choose between two items each.

        Useful for evaluating or ranking outputs (e.g. which of two images better
        matches a prompt). Like classification, it creates a DRAFT job definition
        and only spends once you confirm the cost with the user and call
        ``start_job``. Each pair must hold exactly two asset references from
        ``upload_media`` (never URLs or paths), so (unlike classification) the
        media is required. ``start_job`` picks who answers.
        """
        if not comparison_pairs:
            raise ValueError("comparison_pairs must not be empty")
        for i, pair in enumerate(comparison_pairs):
            if len(pair) != 2:
                raise ValueError(f"comparison_pairs[{i}] must have exactly two items")
        if contexts is not None and len(contexts) != len(comparison_pairs):
            raise ValueError(
                "contexts, when provided, must have one entry per pair "
                "(same length as comparison_pairs)"
            )
        pairs = [
            [_uploaded(r, f"comparison_pairs[{i}][{j}]") for j, r in enumerate(pair)]
            for i, pair in enumerate(comparison_pairs)
        ]

        client = _client()
        definition = client.job.create_compare_job_definition(
            name=name,
            instruction=instruction,
            datapoints=pairs,
            responses_per_datapoint=responses_per_datapoint,
            contexts=contexts,
            confidence_threshold=confidence_threshold,
        )
        return _draft_result(
            client, definition, len(comparison_pairs) * responses_per_datapoint
        )

    @mcp.tool(
        title="Start job",
        annotations=ToolAnnotations(
            title="Start job",
            # Begins collecting and spending, which is not reversible — flag it as
            # the one destructive action.
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=_OPEN_WORLD,
        ),
    )
    def start_job(
        job_definition_id: Annotated[
            str,
            Field(
                description=(
                    "The draft job definition id returned by a create_*_task tool."
                )
            ),
        ],
        audience_id: Annotated[
            str,
            Field(
                description=(
                    'Who answers: "global" (everyone, ready at once), a curated '
                    "audience's id, or the id of an audience the user built in the "
                    "Rapidata dashboard."
                )
            ),
        ] = _GLOBAL_AUDIENCE_ID,
        filters: Annotated[
            Filters | None,
            Field(
                description=(
                    "Optional: narrow the audience to countries (ISO 3166-1 alpha-2, "
                    'e.g. "DE") and/or languages (ISO 639-1, e.g. "de"). '
                    + FILTERS_RULE
                )
            ),
        ] = None,
    ) -> StartedJob:
        """Start a draft job definition on an audience. This step spends.

        Runs the job definition created by ``create_*_task``, which begins
        collecting responses and spending. Only call this after confirming the
        task's cost (``estimated_cost_usd``, or ``total_responses`` when it is
        null) with the user.
        """
        client = _client()
        audience_id = audience_id.strip() or _GLOBAL_AUDIENCE_ID
        try:
            audience = client.audience.get_audience_by_id(audience_id)
        except Exception as e:
            if getattr(e, "status_code", None) not in (400, 403, 404):
                raise
            raise ValueError(
                f"Unknown audience {audience_id!r}. Use \"global\", a curated "
                "audience's id or the id of an audience the user built in the "
                "dashboard."
            ) from e
        if audience_id != _GLOBAL_AUDIENCE_ID:
            _refuse_audience_that_never_answers(client, audience)

        sdk_filters = _sdk_filters(filters or {})
        target: Any = audience
        if sdk_filters:
            narrow = getattr(audience, "filter", None)
            if narrow is None:
                raise ValueError(
                    f"Audience {audience_id!r} takes no filters; start it without."
                )
            target = narrow(sdk_filters)
        job = target.assign_job(_definition(job_definition_id))
        return {
            "job_id": job.id,
            "name": job.name,
            "status": job.get_status(),
            "details_url": job.job_details_page,
            "audience_id": target.id,
            "audience": _describe_audience(audience.name, filters or {}),
            "started": True,
        }

    @mcp.tool(
        title="Get job status",
        annotations=ToolAnnotations(
            title="Get job status",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=_OPEN_WORLD,
        ),
    )
    def get_job_status(
        job_id: Annotated[
            str, Field(description="The job id returned by start_job.")
        ],
    ) -> JobStatus:
        """Get a job's current state and how far along it is, without results.

        Returns ``status`` (the platform state, e.g. ``Submitted``, ``Running``,
        ``ManualApproval``, ``Completed``, ``Paused``, ``Failed``),
        ``completion_percentage`` (0 to 100) and ``details_url``, the job's page in
        the Rapidata dashboard. Use ``get_job_results`` for the responses.
        """
        job = _job(job_id)
        progress = job.get_progress()
        return {
            "job_id": job.id,
            "status": progress.state,
            "completion_percentage": round(progress.completion_percentage, 1),
            "details_url": job.job_details_page,
        }

    @mcp.tool(
        title="Get job results",
        annotations=ToolAnnotations(
            title="Get job results",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=_OPEN_WORLD,
        ),
    )
    def get_job_results(
        job_id: Annotated[
            str, Field(description="The job id returned by start_job.")
        ],
        include_details: Annotated[
            bool,
            Field(
                description=(
                    "Include per-annotator detail (country, language, demographics, "
                    "reliability score). Large; off by default."
                )
            ),
        ] = False,
        max_datapoints: Annotated[
            int,
            Field(
                description="Maximum number of per-datapoint result entries to return."
            ),
        ] = 50,
    ) -> JobResults:
        """Fetch results for a job. Never blocks on a still-running job.

        If the job has finished, returns the final results with ``result_status:
        "complete"``; if it was paused, returns the partial snapshot collected so
        far. If it is still processing, returns a ``result_status`` to poll on
        (e.g. ``not_started``, ``collecting``, ``manual_review``) and no results.
        """
        job = _job(job_id)
        status = job.get_status()

        if status == "Failed":
            return {
                "job_id": job.id,
                "status": status,
                "result_status": "failed",
                "error": job._get_job_failure_message() or "Job failed.",
                "results": None,
            }

        if status not in ("Completed", "Paused"):
            return {
                "job_id": job.id,
                "status": status,
                "result_status": _PENDING_STATES.get(status, "pending"),
                "results": None,
            }

        try:
            results = job.get_results(preliminary_results=status == "Paused")
        except Exception as e:
            # A finished/paused job can briefly have no downloadable file while
            # the first responses are aggregated — surface as pollable, not error.
            return {
                "job_id": job.id,
                "status": status,
                "result_status": "pending",
                "detail": f"Results not available yet: {e}",
                "results": None,
            }

        summary = summarize_results(results, include_details, max_datapoints)
        return {
            "job_id": job.id,
            "status": status,
            "result_status": "complete" if status == "Completed" else "partial",
            "results": summary["results"],
            "info": summary["info"],
            "summary": summary["summary"],
            "datapoint_count": summary["datapoint_count"],
            "returned_datapoints": summary["returned_datapoints"],
            "truncated": summary["truncated"],
        }

    @mcp.tool(
        title="List jobs",
        annotations=ToolAnnotations(
            title="List jobs",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=_OPEN_WORLD,
        ),
    )
    def list_jobs(
        name_contains: Annotated[
            str,
            Field(
                description=(
                    "Only return jobs whose name contains this text; empty "
                    "returns all."
                )
            ),
        ] = "",
        limit: Annotated[
            int, Field(description="Maximum number of jobs to return.")
        ] = 10,
    ) -> JobList:
        """List your most recent jobs, newest first."""
        jobs = _client().job.find_jobs(name=name_contains, amount=limit)
        return {
            "jobs": [
                {
                    "job_id": job.id,
                    "name": job.name,
                    "status": job.get_status(),
                    "details_url": job.job_details_page,
                }
                for job in jobs
            ]
        }

    @mcp.tool(
        title="Get account balance",
        annotations=ToolAnnotations(
            title="Get account balance",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=_OPEN_WORLD,
        ),
    )
    def get_account_balance() -> AccountBalance:
        """How much the user's Rapidata account can still spend, in US dollars.

        ``mode`` is ``prepaid`` (credit, e.g. the $20 every new account gets),
        ``limited`` (billed for usage up to ``spend_limit_usd``) or ``usage``
        (billed for usage). ``budget_left_usd`` is the credit left, or the room
        under the spending cap; null means no cap. ``owed_usd`` is what ended
        billing periods left unpaid.
        """
        return _account_balance(_client())

    @mcp.tool(
        title="Pause job",
        annotations=ToolAnnotations(
            title="Pause job",
            # Stops collecting/spending; reversible (the job can be resumed).
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=_OPEN_WORLD,
        ),
    )
    def pause_job(
        job_id: Annotated[
            str,
            Field(
                description=(
                    "The job_id returned by start_job or list_jobs (not a "
                    "job_definition_id)."
                )
            ),
        ],
    ) -> PausedJob:
        """Pause a running job to stop collecting further responses (and spending).

        ``resume_job`` continues it."""
        job = _job(job_id).pause()
        return {"job_id": job.id, "status": job.get_status(), "paused": True}

    @mcp.tool(
        title="Resume job",
        annotations=ToolAnnotations(
            title="Resume job",
            # Spends again, but only what start_job already allowed.
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=_OPEN_WORLD,
        ),
    )
    def resume_job(
        job_id: Annotated[
            str, Field(description="The job_id of a job pause_job paused.")
        ],
    ) -> PausedJob:
        """Resume a paused job so it collects responses (and spends) again."""
        job = _job(job_id)
        status = job.get_status()
        if status != "Paused":
            raise ValueError(
                f"Job {job.id} is {status}, not Paused; only a paused job resumes."
            )
        job.resume()
        return {"job_id": job.id, "status": job.get_status(), "paused": False}

    _reject_outdated_calls(mcp)


def _reject_outdated_calls(mcp: FastMCP) -> None:
    """Fail a call made from an older tool list instead of dropping the arguments it
    no longer takes: a dropped media argument turns into the placeholder image."""
    manager = mcp._tool_manager
    call_tool = manager.call_tool

    async def checked_call(name: str, arguments: dict[str, Any], *args, **kwargs):
        tool = manager.get_tool(name)
        if tool is None:
            raise ToolError(f"Unknown tool: {name}. {_OUTDATED_TOOLS}")
        unknown = sorted(set(arguments) - set(tool.parameters.get("properties", {})))
        if unknown:
            raise ToolError(f"{name} takes no {', '.join(unknown)}. {_OUTDATED_TOOLS}")
        return await call_tool(name, arguments, *args, **kwargs)

    manager.call_tool = checked_call


def _within(future: Future[Any], deadline: float) -> Any:
    """The future's result, or None when it fails or is not done by ``deadline``."""
    try:
        return future.result(timeout=max(deadline - time.monotonic(), 0.0))
    except FutureTimeout:
        return None
    except Exception:
        logger.warning("Could not price a draft", exc_info=True)
        return None


def _estimated_cost(definition: RapidataJobDefinition) -> float:
    return round(definition.estimated_cost.estimated_cost, 2)


def _account_balance(client: RapidataClient) -> AccountBalance:
    # The billing summary, unlike the SDK's get_current_billing_period, also
    # answers for an account that has no billing period yet.
    summary = client._openapi_service.payment.billing_api.billing_summary_get()
    mode = _BILLING_MODES.get(summary.mode.value, "usage")
    spent = summary.current_period_usage
    limit = summary.effective_limit
    if mode == "prepaid":
        left = summary.available_amount
    elif limit is not None:
        left = max(limit - spent, 0.0)
    else:
        left = None
    try:
        owed: float | None = client.billing.get_outstanding_balance()
    except Exception:
        logger.warning("Could not read the outstanding balance", exc_info=True)
        owed = None
    return {
        "mode": mode,
        "budget_left_usd": _cents(left),
        "spent_this_period_usd": round(spent, 2),
        "spend_limit_usd": _cents(limit),
        "owed_usd": owed,
    }


_BILLING_MODES: dict[str, Literal["prepaid", "limited", "usage"]] = {
    "Prepaid": "prepaid",
    "Limited": "limited",
    "Usage": "usage",
}


def _cents(amount: float | None) -> float | None:
    return None if amount is None else round(amount, 2)


def _sdk_filters(filters: Filters) -> list[Any]:
    from rapidata import CountryFilter, LanguageFilter

    countries = [c.strip().upper() for c in filters.get("countries") or []]
    languages = [code.strip().lower() for code in filters.get("languages") or []]
    sdk_filters: list[Any] = []
    try:
        if countries:
            sdk_filters.append(CountryFilter(countries))
        if languages:
            sdk_filters.append(LanguageFilter(languages))
    except ValueError as e:
        raise ValueError(
            "filters takes two-letter codes: countries as ISO 3166-1 alpha-2 "
            f'("DE"), languages as ISO 639-1 ("de"). {e}'
        ) from e
    return sdk_filters


def _describe_audience(name: str, filters: Filters) -> str:
    parts = [
        f"{key} {', '.join(codes)}"
        for key, codes in (
            ("countries", [c.strip().upper() for c in filters.get("countries") or []]),
            ("languages", [c.strip().lower() for c in filters.get("languages") or []]),
        )
        if codes
    ]
    return f"{name}, filtered to {' and '.join(parts)}" if parts else name


def _refuse_audience_that_never_answers(client: RapidataClient, audience: Any) -> None:
    from rapidata.rapidata_client.audience._rapidata_unlisted_audience import (
        UNLISTED_AUDIENCE_PREFIX,
    )
    from rapidata.rapidata_client.audience.recruiting import (
        audience_will_never_produce_responses,
    )

    if audience.id.startswith(UNLISTED_AUDIENCE_PREFIX):
        return
    api = client._openapi_service.audience.audience_api
    status = api.audience_audience_id_get(audience.id).status
    metrics = audience.get_recruiting_metrics()
    # A curated audience recruits nobody itself, so it reports an empty funnel.
    funnel = (metrics.graduated, metrics.distilling, metrics.dropped, metrics.inactive)
    if not any(funnel):
        metrics = None
    if audience_will_never_produce_responses(status, metrics):
        raise ValueError(
            f"Audience {audience.name!r} ({audience.id}) can never answer: nobody "
            "has qualified and it is not recruiting. Ask the user to finish it in the "
            'Rapidata dashboard, or start the job on "global".'
        )
