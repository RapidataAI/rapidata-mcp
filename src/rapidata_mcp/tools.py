"""The Rapidata MCP tool layer.

Exposes a small, curated set of tools that let an MCP-capable agent run human
labeling tasks on Rapidata: create a classification or comparison job definition
(a draft, no spend), start it on the global audience (the single money-spending
step), poll its status, and fetch shaped results.

The create step never spends: it produces a *draft job definition* and asks the
caller to confirm with the user before ``start_job`` runs it on the global
audience. This mirrors the platform's own split between a job definition (the
template) and a job (a run of that definition against an audience).

Tools resolve their client through a ``provider_factory`` rather than holding a
fixed client, so the hosted transport can hand each call a per-request client
scoped to the authenticated customer (see :mod:`rapidata_mcp.server`). Tool
behaviour is otherwise identical regardless of how the client is resolved.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import TYPE_CHECKING, Annotated, Any

from mcp.types import ToolAnnotations
from pydantic import Field

from rapidata_mcp.auth import ClientProvider
from rapidata_mcp.results import summarize_results

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP
    from rapidata.rapidata_client.job.rapidata_job import RapidataJob
    from rapidata.rapidata_client.job.rapidata_job_definition import (
        RapidataJobDefinition,
    )
    from rapidata.rapidata_client.results.rapidata_results import RapidataResults

# The well-known id of the global audience — the broadest pool of labelers,
# ready to work immediately with no targeting or qualification setup.
_GLOBAL_AUDIENCE_ID = "global"

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
# States from which a (partial or final) result file can be downloaded.
_FINISHED_STATES = {"Completed", "Paused"}

# Every tool reaches the Rapidata platform (an external service), the global
# crowd of human annotators, and caller-supplied public media URLs — an open
# world of external entities, so openWorldHint is true throughout.
_OPEN_WORLD = True


def register_tools(
    mcp: FastMCP, provider_factory: Callable[[], ClientProvider]
) -> None:
    """Register all Rapidata tools on ``mcp``.

    Args:
        mcp: The FastMCP server to register tools on.
        provider_factory: Resolves the :class:`ClientProvider` for the current
            call. Called once per tool invocation so per-request, token-scoped
            clients work without the tools knowing about the auth model.
    """

    def _client():
        return provider_factory().get_client()

    def _job(job_id: str) -> RapidataJob:
        return _client().job.get_job_by_id(job_id)

    def _definition(job_definition_id: str) -> RapidataJobDefinition:
        return _client().job.get_job_definition_by_id(job_definition_id)

    def _download_results(job: RapidataJob) -> RapidataResults:
        # Download the current result file directly rather than via
        # job.get_results(), which blocks until the job reaches a terminal state
        # (a paused job never would). The SDK exposes no non-blocking accessor.
        from rapidata.rapidata_client.results.rapidata_results import RapidataResults

        raw = job._openapi_service.order.job_api.job_job_id_download_results_get(
            job_id=job.id
        )
        return RapidataResults(json.loads(raw))

    def _draft_result(
        definition: RapidataJobDefinition, total_responses: int
    ) -> dict[str, Any]:
        return {
            "job_definition_id": definition.id,
            "name": definition.name,
            "status": "draft",
            # Preview page for the definition before it runs.
            "details_url": definition._job_details_page,
            "runs_on": "global audience",
            "total_responses": total_responses,
            "confirmation_required": True,
            "next_step": (
                "This job definition is a DRAFT and is NOT collecting responses "
                f"yet. Starting it will collect {total_responses} human responses "
                "on the global audience and spend money. Present this cost to the "
                "user and ask whether to start collecting. Only after the user "
                "confirms, call start_job with this job_definition_id."
            ),
        }

    @mcp.tool(
        title="Create classification task",
        annotations=ToolAnnotations(
            title="Create classification task",
            # Creates a reversible draft job definition; no spend, nothing destroyed.
            readOnlyHint=False,
            destructiveHint=False,
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
            Field(description="The options annotators choose from (at least two)."),
        ],
        datapoint_urls: Annotated[
            list[str] | None,
            Field(
                description=(
                    "One publicly reachable URL per item to label "
                    "(image/video/audio). Optional — omit it to ask the instruction "
                    "on its own against a generic placeholder image."
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
                    "Optional early-stop: stop a datapoint once this confidence is "
                    "reached or the response cap is hit."
                )
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Create a classification task where humans pick one answer option per item.

        Creates a DRAFT job definition and does NOT start collecting responses (or
        spend): it returns ``confirmation_required`` and expects you to confirm the
        cost with the user, then call ``start_job``. When run, the task goes to the
        global audience (no targeting).
        """
        if len(answer_options) < 2:
            raise ValueError("answer_options must contain at least two options")

        datapoints = datapoint_urls if datapoint_urls else [_PLACEHOLDER_IMAGE_URL]
        if contexts is not None and len(contexts) != len(datapoints):
            raise ValueError(
                "contexts, when provided, must have one entry per datapoint "
                "(same length as datapoint_urls)"
            )

        definition = _client().job.create_classification_job_definition(
            name=name,
            instruction=instruction,
            answer_options=answer_options,
            datapoints=datapoints,
            responses_per_datapoint=responses_per_datapoint,
            contexts=contexts,
            confidence_threshold=confidence_threshold,
        )
        return _draft_result(definition, len(datapoints) * responses_per_datapoint)

    @mcp.tool(
        title="Create comparison task",
        annotations=ToolAnnotations(
            title="Create comparison task",
            readOnlyHint=False,
            destructiveHint=False,
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
                    "List of [item_a_url, item_b_url] pairs; each pair holds exactly "
                    "two publicly reachable URLs."
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
            Field(description="Optional early-stop per pair once confident enough."),
        ] = None,
    ) -> dict[str, Any]:
        """Create a pairwise comparison task: humans choose between two items each.

        Useful for evaluating or ranking outputs (e.g. which of two images better
        matches a prompt). Like classification, it creates a DRAFT job definition
        and only spends once you confirm the cost with the user and call
        ``start_job``. Each pair must hold exactly two publicly reachable URLs, so
        (unlike classification) the media is required. When run, it goes to the
        global audience.
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

        definition = _client().job.create_compare_job_definition(
            name=name,
            instruction=instruction,
            datapoints=comparison_pairs,
            responses_per_datapoint=responses_per_datapoint,
            contexts=contexts,
            confidence_threshold=confidence_threshold,
        )
        return _draft_result(
            definition, len(comparison_pairs) * responses_per_datapoint
        )

    @mcp.tool(
        title="Start job (spends)",
        annotations=ToolAnnotations(
            title="Start job (spends)",
            # Runs the job on the global audience: begins collecting and spending,
            # which is not reversible — flag it as the one destructive action.
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
    ) -> dict[str, Any]:
        """Start a draft job definition on the global audience. This step spends.

        Runs the job definition created by ``create_*_task`` against the global
        audience, which begins collecting responses and spending. Only call this
        after confirming the task's cost (``total_responses``) with the user.
        """
        audience = _client().audience.get_audience_by_id(_GLOBAL_AUDIENCE_ID)
        job = audience.assign_job(_definition(job_definition_id))
        return {
            "job_id": job.id,
            "name": job.name,
            "status": job.get_status(),
            "details_url": job.job_details_page,
            "started": True,
        }

    @mcp.tool(
        title="Get job status",
        annotations=ToolAnnotations(
            title="Get job status",
            readOnlyHint=True,
            destructiveHint=False,
            openWorldHint=_OPEN_WORLD,
        ),
    )
    def get_job_status(
        job_id: Annotated[
            str, Field(description="The job id returned by start_job.")
        ],
    ) -> dict[str, Any]:
        """Get the current status of a running job without fetching results."""
        job = _job(job_id)
        return {
            "job_id": job.id,
            "status": job.get_status(),
            "details_url": job.job_details_page,
        }

    @mcp.tool(
        title="Get job results",
        annotations=ToolAnnotations(
            title="Get job results",
            readOnlyHint=True,
            destructiveHint=False,
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
    ) -> dict[str, Any]:
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

        if status not in _FINISHED_STATES:
            return {
                "job_id": job.id,
                "status": status,
                "result_status": _PENDING_STATES.get(status, "pending"),
                "results": None,
            }

        try:
            results = _download_results(job)
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

        return {
            "job_id": job.id,
            "status": status,
            "result_status": "complete" if status == "Completed" else "partial",
            **summarize_results(results, include_details, max_datapoints),
        }

    @mcp.tool(
        title="List jobs",
        annotations=ToolAnnotations(
            title="List jobs",
            readOnlyHint=True,
            destructiveHint=False,
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
    ) -> dict[str, Any]:
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
            str, Field(description="The job id to pause.")
        ],
    ) -> dict[str, Any]:
        """Pause a running job to stop collecting further responses (and spending)."""
        job = _job(job_id)
        # The SDK's RapidataJob has no public pause wrapper yet, so call the job API.
        job._openapi_service.order.job_api.job_job_id_pause_post(job.id)
        return {"job_id": job.id, "status": job.get_status(), "paused": True}
