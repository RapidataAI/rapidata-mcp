"""The Rapidata MCP tool layer.

Exposes a small, curated set of tools that let an MCP-capable agent run human
labeling tasks on Rapidata: create a classification or comparison task, start
it (the single money-spending step), poll its status, and fetch shaped results.

Tools resolve their client through a ``provider_factory`` rather than holding a
fixed client, so the hosted transport can hand each call a per-request client
scoped to the authenticated customer (see :mod:`rapidata_mcp.server`). Tool
behaviour is otherwise identical regardless of how the client is resolved.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from rapidata_mcp.auth import ClientProvider
from rapidata_mcp.results import summarize_results

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP
    from rapidata.rapidata_client.order.rapidata_order import RapidataOrder

# States in which an order is not yet producing results. Mapped to the
# result_status an agent should poll on, so get_task_results never blocks
# waiting for a long-running or stalled order.
_PENDING_STATES: dict[str, str] = {
    "Created": "not_started",
    "Preview": "not_started",
    "Submitted": "in_review",
    "ManualReview": "manual_review",
    "StaleResults": "regenerating",
}
# States from which the final result file is available immediately.
_FINISHED_STATES = {"Completed", "Paused"}


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

    def _order(order_id: str) -> RapidataOrder:
        return _client().order.get_order_by_id(order_id)

    @mcp.tool()
    def create_classification_task(
        name: str,
        instruction: str,
        answer_options: list[str],
        datapoint_urls: list[str],
        responses_per_datapoint: int = 10,
        confidence_threshold: float | None = None,
    ) -> dict[str, Any]:
        """Create a classification task where humans pick one answer option per item.

        The task is created in draft and does NOT start collecting responses (or
        spend) until you call ``run_task``. Datapoints must be publicly
        reachable URLs (image/video/audio, or plain text). Returns the order id,
        a details URL to inspect it, and ``total_responses`` — the number of
        human responses that will be collected (datapoints x
        responses_per_datapoint), which is what drives cost.

        Args:
            name: Internal label for the task (not shown to annotators).
            instruction: What annotators should do.
            answer_options: The options annotators choose from (at least two).
            datapoint_urls: One URL per item to be labeled.
            responses_per_datapoint: Human responses collected per item.
            confidence_threshold: Optional early-stop; stops a datapoint once
                this confidence is reached or the response cap is hit.
        """
        if len(answer_options) < 2:
            raise ValueError("answer_options must contain at least two options")
        if not datapoint_urls:
            raise ValueError("datapoint_urls must not be empty")

        order = _client().order.create_classification_order(
            name=name,
            instruction=instruction,
            answer_options=answer_options,
            datapoints=datapoint_urls,
            responses_per_datapoint=responses_per_datapoint,
            confidence_threshold=confidence_threshold,
        )
        return {
            "order_id": order.id,
            "name": order.name,
            "status": order.get_status(),
            "details_url": order.order_details_page,
            "total_responses": len(datapoint_urls) * responses_per_datapoint,
            "next_step": "Call run_task to start collecting responses (this spends).",
        }

    @mcp.tool()
    def create_comparison_task(
        name: str,
        instruction: str,
        comparison_pairs: list[list[str]],
        responses_per_datapoint: int = 10,
        confidence_threshold: float | None = None,
    ) -> dict[str, Any]:
        """Create a pairwise comparison task: humans choose between two items each.

        Useful for evaluating or ranking outputs (e.g. which of two images
        better matches a prompt). Like classification, it is created in draft and
        only spends once ``run_task`` is called. Each pair must hold exactly two
        publicly reachable URLs.

        Args:
            name: Internal label for the task (not shown to annotators).
            instruction: The question annotators answer for each pair.
            comparison_pairs: List of [item_a_url, item_b_url] pairs.
            responses_per_datapoint: Human responses collected per pair.
            confidence_threshold: Optional early-stop per pair.
        """
        if not comparison_pairs:
            raise ValueError("comparison_pairs must not be empty")
        for i, pair in enumerate(comparison_pairs):
            if len(pair) != 2:
                raise ValueError(f"comparison_pairs[{i}] must have exactly two items")

        order = _client().order.create_compare_order(
            name=name,
            instruction=instruction,
            datapoints=comparison_pairs,
            responses_per_datapoint=responses_per_datapoint,
            confidence_threshold=confidence_threshold,
        )
        return {
            "order_id": order.id,
            "name": order.name,
            "status": order.get_status(),
            "details_url": order.order_details_page,
            "total_responses": len(comparison_pairs) * responses_per_datapoint,
            "next_step": "Call run_task to start collecting responses (this spends).",
        }

    @mcp.tool()
    def run_task(order_id: str) -> dict[str, Any]:
        """Start collecting responses for a created task. This is the step that spends.

        Only call this after confirming the task's cost (``total_responses``) and
        reviewing its ``details_url`` is acceptable.
        """
        order = _order(order_id)
        order.run()
        return {"order_id": order.id, "status": order.get_status(), "started": True}

    @mcp.tool()
    def get_task_status(order_id: str) -> dict[str, Any]:
        """Get the current status of a task without fetching results."""
        order = _order(order_id)
        return {
            "order_id": order.id,
            "status": order.get_status(),
            "details_url": order.order_details_page,
        }

    @mcp.tool()
    def get_task_results(
        order_id: str,
        include_details: bool = False,
        max_datapoints: int = 50,
    ) -> dict[str, Any]:
        """Fetch results for a task. Never blocks on a long-running task.

        If the task is still processing, returns the partial snapshot collected
        so far with ``result_status: "partial"``; if it has finished, returns the
        final results with ``result_status: "complete"``. If it has not started
        producing results yet, returns a ``result_status`` to poll on (e.g.
        ``not_started``, ``in_review``) and no results.

        Args:
            order_id: The task to fetch.
            include_details: Include per-annotator detail (country, language,
                demographics, reliability score). Large; off by default.
            max_datapoints: Cap on per-datapoint entries returned.
        """
        order = _order(order_id)
        status = order.get_status()

        if status in _PENDING_STATES:
            return {
                "order_id": order.id,
                "status": status,
                "result_status": _PENDING_STATES[status],
                "results": None,
            }

        if status == "Failed":
            return {
                "order_id": order.id,
                "status": status,
                "result_status": "failed",
                "error": order._get_order_failure_message() or "Order failed.",
                "results": None,
            }

        finished = status in _FINISHED_STATES
        try:
            results = order.get_results(preliminary_results=not finished)
        except Exception as e:
            # Partial snapshots can be briefly unavailable while the first
            # responses are still being aggregated — surface as pollable, not error.
            return {
                "order_id": order.id,
                "status": status,
                "result_status": "pending",
                "detail": f"Results not available yet: {e}",
                "results": None,
            }

        return {
            "order_id": order.id,
            "status": status,
            "result_status": "complete" if finished else "partial",
            **summarize_results(results, include_details, max_datapoints),
        }

    @mcp.tool()
    def list_tasks(name_contains: str = "", limit: int = 10) -> dict[str, Any]:
        """List your most recent tasks, newest first."""
        orders = _client().order.find_orders(name=name_contains, amount=limit)
        return {
            "tasks": [
                {
                    "order_id": o.id,
                    "name": o.name,
                    "status": o.get_status(),
                    "details_url": o.order_details_page,
                }
                for o in orders
            ]
        }

    @mcp.tool()
    def pause_task(order_id: str) -> dict[str, Any]:
        """Pause a running task to stop collecting further responses (and spending)."""
        order = _order(order_id)
        order.pause()
        return {"order_id": order.id, "status": order.get_status(), "paused": True}
