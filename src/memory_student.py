from __future__ import annotations

from typing import Any

from .config import settings
from .context_budget import ContextBudgetManager
from .utils import cap_query, join_nonempty
from .zep_common import prime_eval_thread, render_graph_search


class StudentMemory:
    """Only this file needs to be edited by students."""

    def __init__(self, client: Any):
        self.client = client
        self.budget = ContextBudgetManager(settings.context_tokens)

    # NOTE: Zep rejects graph.search queries longer than 400 characters. Some
    # eval queries are longer than that, so wrap every query with
    # `cap_query(query)` (see src/utils.py) before passing it to graph.search.

    def retrieve_long_term(self, user_id: str, thread_id: str, query: str) -> str:
        # LAB TODO 1/4 — done.
        # Context Block needs a fresh thread slice with the current query so
        # Zep can rank relevance against it before we ask for user context.
        prime_eval_thread(self.client, user_id, thread_id, query)
        user_context = self.client.thread.get_user_context(thread_id=thread_id)
        context_block = getattr(user_context, "context", "") or ""

        # Bonus: explicit fact/edge search with validity ranges. The Context
        # Block alone can drop a low-salience open-loop fact (e.g. a deadline
        # mentioned once); an edges search with a higher limit surfaces it
        # with valid_at/invalid_at so recency conflicts (E08) are auditable.
        try:
            facts = self.client.graph.search(
                user_id=user_id,
                query=cap_query(query),
                scope="edges",
                limit=20,
            )
            fact_text = render_graph_search(facts)
        except Exception:
            fact_text = ""
        return join_nonempty([context_block, fact_text], sep="\n\n")

    def retrieve_episodic(self, user_id: str, query: str) -> str:
        # LAB TODO 2/4 — done.
        # user_id (not graph_id): episodes live on the user's own graph.
        # episode_char_cap trims each episode so several distinct trajectories
        # fit the tight episodic budget instead of one verbose session episode
        # crowding everything else out.
        results = self.client.graph.search(
            user_id=user_id,
            query=cap_query(query),
            scope="episodes",
            limit=15,
        )
        return render_graph_search(results, episode_char_cap=180)

    def retrieve_semantic(self, graph_id: str, query: str) -> str:
        # LAB TODO 3/4 — done.
        # Search the standalone graph (graph_id, NOT user_id).
        # scope="episodes" returns raw document text that keeps literal
        # markers (e.g. PAYMENT-RULE-3, CONN-POOL-FIRST). scope="auto" returns
        # extracted facts that drop those literal codes, so avoid it here.
        q = cap_query(query)
        try:
            results = self.client.graph.search(
                graph_id=graph_id,
                query=q,
                scope="episodes",
                limit=8,
            )
        except Exception:
            # Fallback for accounts/SDK versions where "episodes" scope
            # behaves differently on a standalone (non-user) graph.
            results = self.client.graph.search(
                graph_id=graph_id,
                query=q,
                scope="nodes",
                limit=8,
            )
        return render_graph_search(results)

    def assemble_context(self, layers: dict[str, str]) -> tuple[str, dict[str, dict[str, int]]]:
        # LAB TODO 4/4 — done.
        # ContextBudgetManager already encodes the 10/4/3/3 token budget and
        # the short_term -> long_term -> episodic -> semantic priority order.
        return self.budget.assemble(layers)
