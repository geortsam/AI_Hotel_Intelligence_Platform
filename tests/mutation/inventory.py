"""The mutation checks the V2 stage reports recorded, as data.

Every entry reproduces a mutation a stage report says was run and caught. The edits are the ones
those runs applied (recovered from the scripts that ran them), and the killers are tests the
runs recorded as failing. Nothing here is a new acceptance criterion. Where an entry departs
from the original run, its ``note`` says how and why.

| Stage | Recorded | Here | Notes |
|---|---|---|---|
| 7.6 tool boundary | 5 | 5 | M5 was malformed originally; see its note |
| 7.7 copilot | 8 runs | 7 | two recorded labels were the same edit |
| 7.8 evaluation | 7 | 7 | |
| 7.9 documents | 1 | 1 | |
| 7.10 grounded retrieval | 5 | 5 | |
| 7.11 conversations | 7 | 7 | |
| 7.12 attention list | 8 | 8 | |
| 7.13 copilot screen | 14 | 14 | Vitest |
| 7.14 multi-horizon | 10 | 10 | M1 disables both short-lag guards, as its re-run did |
| F2 purge command | 3 | 3 | PostgreSQL |
| F16 frontend contract | 12 | 12 | |
| I4 invocation retention | 14 | 14 | PostgreSQL except M13 and M14 |
| F1 hotel-local booking days | 8 | 8 | PostgreSQL |
| F2-ADR dashboard ADR caption | 6 | 6 | Vitest; "F2" was already the purge command's stage |
| M1 runbook schema facts | 8 | 8 | |
| M2 current-state docs | 8 | 8 | |
| F2-COMP complimentary-only ADR reason | 6 | 6 | Vitest |
| F3 occupancy change in points | 8 | 8 | Vitest |
| F4 arrivals and departures by status | 8 | 8 | PostgreSQL M1-M5, Vitest M6-M7 |
"""

from __future__ import annotations

from tests.mutation.harness import Edit, Mutation, Runner

# --- paths -------------------------------------------------------------------------------------

TOOL_INVOCATION = "backend/app/services/tool_invocation.py"
COPILOT_SERVICE = "backend/app/services/copilot.py"
CONVERSATION_SERVICE = "backend/app/services/copilot_conversation.py"
CONVERSATION_REPOSITORY = "backend/app/repositories/copilot_conversation.py"
INSIGHT_SERVICE = "backend/app/services/insight.py"
HORIZONS = "ml/horizons.py"
OFFLINE_DEMAND = "ml/pipelines/offline_demand.py"
FE_COPILOT = "frontend/src/features/copilot/"
CONFIG = "backend/app/core/config.py"
INVOCATION_REPOSITORY = "backend/app/repositories/llm_invocation.py"
INVOCATION_RETENTION = "backend/app/services/llm_invocation_retention.py"
INVOCATION_MIGRATION = "database/migrations/versions/20261003_0017_llm_invocation_retention.py"
ANALYTICS_REPOSITORY = "backend/app/repositories/analytics.py"
INTELLIGENCE_SERVICE = "backend/app/services/intelligence.py"
DASHBOARD_PAGE = "frontend/src/pages/DashboardPage.tsx"
ANALYTICS_PAGE = "frontend/src/pages/AnalyticsPage.tsx"
BACKUP_RUNBOOK = "docs/deployment/backup-restore.md"
BOOTSTRAP_RUNBOOK = "docs/deployment/first-run-bootstrap.md"
COPILOT_FRONTEND_DOC = "docs/copilot-frontend.md"
ML_SERVING_DOC = "docs/ml-serving.md"

TOOL_BOUNDARY = "tests/backend/test_tool_boundary.py"
COPILOT_TESTS = "tests/backend/test_copilot.py"
EVAL_HARNESS = "tests/evaluation/test_harness.py"
EVAL_SCORERS = "tests/evaluation/test_scorers.py"
GROUNDED = "tests/backend/test_grounded_retrieval.py"
CONVERSATION_TESTS = "tests/backend/test_copilot_conversation.py"
CONVERSATION_API = "tests/integration/test_copilot_conversations_api.py"
PRIORITIES = "tests/backend/test_priorities.py"
MULTI_HORIZON = "tests/ml/test_multi_horizon.py"
MULTI_HORIZON_REFIT = "tests/ml/test_multi_horizon_integration.py"
PURGE_JOB = "tests/integration/test_conversation_purge_job.py"
INVOCATION_PURGE = "tests/integration/test_llm_invocation_purge.py"
INVOCATION_STATIC = "tests/backend/test_llm_invocation_retention.py"
BUCKETING = "tests/integration/test_business_day_bucketing.py"
BUSINESS_DAY_GUARD = "tests/backend/test_business_day_guard.py"
DASHBOARD_TESTS = "src/features/dashboard/dashboard.test.tsx"
ANALYTICS_TESTS = "src/features/analytics/analytics.test.tsx"
RUNBOOK_FACTS = "tests/backend/test_runbook_schema_facts.py"
CURRENT_STATE_DOCS = "tests/backend/test_current_state_docs.py"
CONTRACT = "tests/backend/test_frontend_contract.py"
ARCH = "src/features/copilot/architecture.node.test.ts"
SCREEN = "src/features/copilot/copilot.test.tsx"
#: The copilot screen's test titles use the typographic apostrophe (U+2019).
TYPOGRAPHIC_APOSTROPHE = chr(0x2019)


def one(path: str, old: str, new: str) -> tuple[Edit, ...]:
    return (Edit(path, old, new),)


STAGE_7_6 = [
    Mutation(
        "7.6-M1",
        "7.6",
        "the role check is skipped",
        one(
            TOOL_INVOCATION,
            "            self._scope.require_hotel_with_role(hotel_public_id, contract.min_role)\n"
            "        except AppError as refused:",
            "            pass\n        except AppError as refused:",
        ),
        (f"{TOOL_BOUNDARY}::test_authorization_happens_before_validation_and_before_execution",),
    ),
    Mutation(
        "7.6-M2",
        "7.6",
        "the tenant-identifier schema check is removed",
        one(
            "backend/app/copilot/registry.py",
            "            if _names_an_identifier(field_name, FORBIDDEN_INPUT_FRAGMENTS):",
            "            if False:",
        ),
        (f"{TOOL_BOUNDARY}::test_an_input_naming_a_tenant_is_refused_at_registration",),
    ),
    Mutation(
        "7.6-M3",
        "7.6",
        "the failure bound is off by one",
        one(
            "backend/app/copilot/loop.py",
            "                    if failures >= self._max_failures:",
            "                    if failures > self._max_failures:",
        ),
        (f"{TOOL_BOUNDARY}::test_4_a_second_failure_ends_the_loop_immediately",),
    ),
    Mutation(
        "7.6-M4",
        "7.6",
        "the circuit breaker is never consulted",
        one(
            "backend/app/llm/boundary.py",
            "        probe = self._breaker.acquire()",
            "        probe = False",
        ),
        (
            f"{TOOL_BOUNDARY}::"
            "test_the_guard_opens_on_unavailability_and_then_never_reaches_the_provider",
        ),
    ),
    Mutation(
        "7.6-M5",
        "7.6",
        "a tool takes its hotel from somewhere other than the context",
        one(
            "backend/app/copilot/tools/hotel_kpis.py",
            "        context.hotel_public_id, arguments.date_from, arguments.date_to",
            "        __import__('uuid').UUID(int=0), arguments.date_from, arguments.date_to",
        ),
        (f"{TOOL_BOUNDARY}::test_a_tool_passes_the_context_hotel_and_never_an_argument_hotel",),
        note=(
            "The original run prepended `import uuid` above `from __future__`, so the module "
            "failed to import and no named test failed; it was reported caught on the exit "
            "code alone. Here the same substitution is made inline, so the module imports and "
            "a named test must fail."
        ),
    ),
]

STAGE_7_7 = [
    Mutation(
        "7.7-M1",
        "7.7",
        "the budget no longer depends on membership (charged before authorization)",
        one(
            "backend/app/api/deps.py",
            "    current_user: CurrentUserDep,\n"
            "    _member: Annotated[None, Depends(require_copilot_member)],\n"
            ") -> None:\n"
            '    """Charge the per-actor, then the per-hotel, copilot allowance.',
            "    current_user: CurrentUserDep,\n"
            ") -> None:\n"
            '    """Charge the per-actor, then the per-hotel, copilot allowance.',
        ),
        (f"{COPILOT_TESTS}::test_the_budget_cannot_run_before_membership_is_established",),
        note=(
            "Recorded twice: first as 'hotel budget charged before authorization' (missed, "
            "which led to the structural test), then as 'sub-dependency removed' (caught). "
            "It is one edit. Stage 7.11 later gave another dependency the same parameter line, "
            "so the anchor now includes the end of `copilot_budget`'s signature."
        ),
    ),
    Mutation(
        "7.7-M2",
        "7.7",
        "the figure check is disabled",
        one(
            "backend/app/copilot/grounding.py",
            "                offending.append(written)\n",
            "                pass\n",
        ),
        (f"{COPILOT_TESTS}::test_a_figure_no_tool_returned_is_refused",),
        note=(
            "Stage 7.10 rewrote the check around per-sentence evidence; the break is the same: "
            "an ungrounded figure is never recorded as offending."
        ),
    ),
    Mutation(
        "7.7-M3",
        "7.7",
        "a model failure before any tool is swallowed as a 200",
        one(
            COPILOT_SERVICE,
            "        if error is not None and not result.outcomes:",
            "        if False:",
        ),
        (f"{COPILOT_TESTS}::test_a_model_failure_before_any_tool_is_recorded_and_re_raised",),
    ),
    Mutation(
        "7.7-M4",
        "7.7",
        "Retry-After is dropped",
        one(
            "backend/app/core/errors.py",
            '            response.headers["Retry-After"] = str(retry_after)',
            "            pass",
        ),
        (f"{COPILOT_TESTS}::test_a_spent_allowance_carries_retry_after",),
    ),
    Mutation(
        "7.7-M5",
        "7.7",
        "the executor accepts every tool, not the offered set",
        one(
            COPILOT_SERVICE,
            "                hotel_public_id, call.name, call.arguments, offered=offered",
            "                hotel_public_id, call.name, call.arguments, "
            "offered=self._registry.names()",
        ),
        (
            f"{COPILOT_TESTS}::test_every_tool_call_is_bound_to_the_path_hotel_whatever_the_model_says",
        ),
    ),
    Mutation(
        "7.7-M6",
        "7.7",
        "a refused actor still charges the hotel",
        one(
            "backend/app/api/deps.py",
            "    if not actor.allowed:\n"
            "        raise LlmBudgetExhaustedError(COPILOT_BUDGET_MESSAGE, "
            "retry_after=actor.retry_after)\n"
            "\n    hotel = limiter.check(",
            "    hotel = limiter.check(",
        ),
        (
            "tests/integration/test_copilot_api.py::test_the_actor_allowance_is_enforced_with_retry_after",
        ),
        needs_database=True,
    ),
    Mutation(
        "7.7-M7",
        "7.7",
        "the route runs the budget before the membership check",
        one(
            "backend/app/api/v1/endpoints/copilot.py",
            "dependencies=[Depends(require_copilot_member), Depends(copilot_budget)]",
            "dependencies=[Depends(copilot_budget), Depends(require_copilot_member)]",
        ),
        (f"{COPILOT_TESTS}::test_the_budget_cannot_run_before_membership_is_established",),
    ),
]

STAGE_7_8 = [
    Mutation(
        "7.8-M1",
        "7.8",
        "the grounding scorer accepts everything",
        one(
            "tests/evaluation/scorers.py",
            "    return any(\n        rounds_to(source, written, target) or rounds_to(source * "
            "HUNDRED, written, target)",
            "    return True or any(\n        rounds_to(source, written, target) or "
            "rounds_to(source * HUNDRED, written, target)",
        ),
        (f"{EVAL_HARNESS}::test_a_hallucinated_figure_is_detected_and_withheld",),
    ),
    Mutation(
        "7.8-M2",
        "7.8",
        "the tool-selection scorer ignores extra calls",
        one(
            "tests/evaluation/scorers.py",
            "    return every_expected_made and nothing_extra",
            "    return every_expected_made",
        ),
        (f"{EVAL_SCORERS}::test_a_wrong_selection_is_detected[extra_tool]",),
    ),
    Mutation(
        "7.8-M3",
        "7.8",
        "the refusal scorer ignores stated figures",
        one(
            "tests/evaluation/scorers.py",
            "    return complete and states_no_new_figure and stays_in_bounds",
            "    return complete and stays_in_bounds",
        ),
        (f"{EVAL_SCORERS}::test_a_wrong_decline_is_detected[states_a_figure]",),
    ),
    Mutation(
        "7.8-M4",
        "7.8",
        "the copilot stops withholding ungrounded answers",
        one(COPILOT_SERVICE, "        if offending and result.complete:", "        if False:"),
        (f"{EVAL_HARNESS}::test_a_hallucinated_figure_is_detected_and_withheld",),
    ),
    Mutation(
        "7.8-M5",
        "7.8",
        "viewers are offered the manager-only tool",
        one(
            TOOL_INVOCATION,
            "                if refused.code != REFUSED_CODE:\n                    raise\n"
            "                continue\n            permitted.append(contract.name)",
            "                if refused.code != REFUSED_CODE:\n                    raise\n"
            "            permitted.append(contract.name)",
        ),
        (f"{EVAL_HARNESS}::test_the_reference_run_matches_the_pinned_report",),
    ),
    Mutation(
        "7.8-M6",
        "7.8",
        "the served prompt is edited",
        one(
            "backend/app/llm/prompts/registry.py",
            "reply exactly: Not found in this hotel's documents. \"\n"
            '        "Answer concisely, in plain text, in the language of the question."',
            "reply exactly: Not found in this hotel's documents. \"\n"
            '        "Answer concisely, in plain text."',
        ),
        (f"{EVAL_HARNESS}::test_the_reference_run_matches_the_pinned_report",),
        note=(
            "Originally an edit to copilot_answer@v1, the prompt the copilot served at Stage "
            "7.8. Since Stage 7.10 it serves copilot_answer@v2, which the pinned report now "
            "names, and the sentence occurs in four prompts; the edit is anchored in v2."
        ),
    ),
    Mutation(
        "7.8-M7",
        "7.8",
        "the loop allows no tool round",
        one("backend/app/copilot/loop.py", "MAX_TOOL_ROUNDS = 3", "MAX_TOOL_ROUNDS = 0"),
        (f"{EVAL_HARNESS}::test_the_reference_run_matches_the_pinned_report",),
    ),
]

STAGE_7_9 = [
    Mutation(
        "7.9-M1",
        "7.9",
        "the knowledge search drops its hotel filter",
        one(
            "backend/app/repositories/knowledge.py",
            "                HotelDocument.hotel_id == hotel_id,\n"
            "                HotelDocument.status == DocumentStatus.ACTIVE.value,\n"
            '                HotelDocumentChunk.search_vector.op("@@")(tsquery),',
            "                HotelDocument.status == DocumentStatus.ACTIVE.value,\n"
            '                HotelDocumentChunk.search_vector.op("@@")(tsquery),',
        ),
        (
            "tests/backend/test_knowledge.py::test_the_search_filters_hotel_and_status_in_the_where_clause",
        ),
        note=(
            "The original run recorded three failures, two of them against PostgreSQL; this "
            "names the database-free one, so the check runs without a database."
        ),
    ),
]

STAGE_7_10 = [
    Mutation(
        "7.10-M1",
        "7.10",
        "invalid citations are accepted",
        one(
            COPILOT_SERVICE,
            '        if check.invalid:\n            return "citation_rejected"\n',
            "",
        ),
        (f"{GROUNDED}::test_a_citation_no_search_returned_replaces_the_answer",),
    ),
    Mutation(
        "7.10-M2",
        "7.10",
        "the not-found rule is removed",
        one(
            COPILOT_SERVICE,
            '        if searched and not check.cited:\n            return "not_found"\n',
            "",
        ),
        (f"{GROUNDED}::test_a_search_with_no_results_is_not_found_whatever_the_model_claims",),
    ),
    Mutation(
        "7.10-M3",
        "7.10",
        "staged labels are citable before admission",
        one(
            "backend/app/copilot/citations.py",
            "        return self._admitted.get(label)\n",
            "        return self._admitted.get(label) or self._staged.get(label)\n",
        ),
        (
            f"{GROUNDED}::test_the_output_is_a_labelled_untrusted_section_with_labels_not_identifiers",
        ),
    ),
    Mutation(
        "7.10-M4",
        "7.10",
        "documents ground every sentence",
        one(
            COPILOT_SERVICE,
            "                    if o.succeeded and o.output is not None and o.tool != "
            "KNOWLEDGE_TOOL\n",
            "                    if o.succeeded and o.output is not None\n",
        ),
        (
            f"{GROUNDED}::test_a_figure_from_an_excerpt_is_grounded_only_where_that_excerpt_is_cited",
        ),
    ),
    Mutation(
        "7.10-M5",
        "7.10",
        "the knowledge tool ignores the context hotel",
        one(
            "backend/app/copilot/tools/knowledge_search.py",
            "        context.hotel_public_id, arguments.query, arguments.limit\n",
            "        __import__('uuid').UUID(int=0), arguments.query, arguments.limit\n",
        ),
        (f"{GROUNDED}::test_the_search_runs_for_the_path_hotel_whatever_the_model_asks",),
    ),
]

STAGE_7_11 = [
    Mutation(
        "7.11-M1",
        "7.11",
        "ownership is dropped from the conversation read",
        one(
            CONVERSATION_REPOSITORY,
            "                CopilotConversation.actor_user_id == actor_user_id,\n"
            "                CopilotConversation.public_id == public_id,",
            "                CopilotConversation.public_id == public_id,",
        ),
        (
            f"{CONVERSATION_TESTS}::test_every_read_and_delete_filters_hotel_owner_and_retention_in_sql[get]",
        ),
    ),
    Mutation(
        "7.11-M2",
        "7.11",
        "retention is dropped from the conversation read",
        one(
            CONVERSATION_REPOSITORY,
            "                CopilotConversation.public_id == public_id,\n                "
            "_live(retention_days),",
            "                CopilotConversation.public_id == public_id,",
        ),
        (
            f"{CONVERSATION_TESTS}::test_every_read_and_delete_filters_hotel_owner_and_retention_in_sql[get]",
        ),
    ),
    Mutation(
        "7.11-M3",
        "7.11",
        "the history budget is ignored",
        one(
            "backend/app/copilot/history.py",
            "        if len(kept) == MAX_EARLIER_TURNS:\n            break",
            "        pass",
        ),
        (f"{CONVERSATION_TESTS}::test_at_most_the_six_most_recent_turns_are_kept_oldest_first",),
    ),
    Mutation(
        "7.11-M4",
        "7.11",
        "the citation label offset is ignored",
        one(
            COPILOT_SERVICE,
            "        ledger = EvidenceLedger(first_label=first_label)\n",
            "        ledger = EvidenceLedger()\n",
        ),
        (f"{CONVERSATION_TESTS}::test_labels_continue_from_the_first_label_given",),
    ),
    Mutation(
        "7.11-M5",
        "7.11",
        "a continuation is charged before ownership is checked",
        one(
            "backend/app/api/v1/endpoints/copilot_conversations.py",
            "    dependencies=[Depends(require_copilot_member), "
            "Depends(conversation_turn_budget)],",
            "    dependencies=[Depends(require_copilot_member), Depends(copilot_budget)],",
        ),
        (f"{CONVERSATION_TESTS}::test_writes_require_membership_and_model_calls_are_budgeted",),
    ),
    Mutation(
        "7.11-M6",
        "7.11",
        "a write no longer purges expired conversations first",
        one(
            CONVERSATION_SERVICE,
            "        hotel = self._scope.require_hotel(hotel_public_id)\n"
            "        self._retention.purge(hotel_id=hotel.id)\n\n"
            "        answered = self._copilot.answer_turn(",
            "        hotel = self._scope.require_hotel(hotel_public_id)\n\n"
            "        answered = self._copilot.answer_turn(",
        ),
        (f"{CONVERSATION_API}::test_starting_a_conversation_purges_expired_ones_at_that_hotel",),
        needs_database=True,
    ),
    Mutation(
        "7.11-M7",
        "7.11",
        "citation labels are not stripped from history",
        one(
            "backend/app/copilot/history.py",
            "    answer = strip_citations(served_answer)\n",
            "    answer = served_answer\n",
        ),
        (f"{CONVERSATION_TESTS}::test_citation_labels_are_stripped_from_earlier_answers",),
    ),
]

STAGE_7_12 = [
    Mutation(
        "7.12-M1",
        "7.12",
        "peak days are ranked quietest first",
        one(
            INSIGHT_SERVICE,
            "key=lambda p: (-(p.predicted_room_nights or Decimal(0)), p.date)",
            "key=lambda p: ((p.predicted_room_nights or Decimal(0)), p.date)",
        ),
        (f"{PRIORITIES}::test_peak_days_are_the_k_busiest_by_forecast_with_ties_broken_by_date",),
    ),
    Mutation(
        "7.12-M2",
        "7.12",
        "a figure is read from the wrong field",
        one(
            INSIGHT_SERVICE,
            '                    name="on_the_books_room_nights",\n'
            "                    value=_text(point.on_the_books_room_nights),",
            '                    name="on_the_books_room_nights",\n'
            "                    value=_text(point.available_room_nights),",
        ),
        (f"{PRIORITIES}::test_every_peak_figure_is_the_forecast_points_own_value",),
    ),
    Mutation(
        "7.12-M3",
        "7.12",
        "a template carries an imperative",
        one(
            INSIGHT_SERVICE,
            "ANOMALY_LIMITATION = (\n"
            '    "A statistical flag over the observation window; it records that the day '
            'differed, not why."\n'
            ")",
            'ANOMALY_LIMITATION = (\n    "A statistical flag; consider adjusting rates for '
            'such days."\n)',
        ),
        (f"{PRIORITIES}::test_no_template_carries_an_instruction_or_an_operational_act",),
    ),
    Mutation(
        "7.12-M4",
        "7.12",
        "the priorities tool ignores the context hotel",
        one(
            "backend/app/copilot/tools/priorities.py",
            "        context.hotel_public_id, arguments.date_from, arguments.date_to\n",
            "        __import__('uuid').UUID(int=1), arguments.date_from, arguments.date_to\n",
        ),
        (f"{PRIORITIES}::test_the_tool_uses_the_context_hotel_and_returns_the_list_without_it",),
    ),
    Mutation(
        "7.12-M5",
        "7.12",
        "the evaluation baseline reads after the origin",
        one(
            "tests/evaluation/insight_ranking.py",
            "        known = origin - dt.timedelta(days=(origin.weekday() - day.weekday()) % 7)\n",
            "        known = day - dt.timedelta(days=7)\n",
        ),
        (f"{PRIORITIES}::test_the_baseline_never_reads_a_day_after_the_origin",),
    ),
    Mutation(
        "7.12-M6",
        "7.12",
        "the NDCG discount is off by one",
        one(
            "tests/evaluation/insight_ranking.py",
            "math.log2(position + 2)",
            "math.log2(position + 1.5)",
        ),
        (f"{PRIORITIES}::test_ndcg_at_k_matches_a_hand_computation",),
    ),
    Mutation(
        "7.12-M7",
        "7.12",
        "the forecast is trained on another window",
        one(
            INSIGHT_SERVICE,
            "            hotel_public_id, horizon_from, horizon_to, TRAINING_DAYS\n",
            "            hotel_public_id, horizon_from, horizon_to, 30\n",
        ),
        (f"{PRIORITIES}::test_it_reads_exactly_the_window_and_the_protocols_horizon",),
    ),
    Mutation(
        "7.12-M8",
        "7.12",
        "the hotel is not resolved first",
        (
            Edit(
                INSIGHT_SERVICE,
                "        hotel = self._scope.require_hotel(hotel_public_id)\n        anomalies",
                "        anomalies",
            ),
            Edit(
                INSIGHT_SERVICE,
                "hotel_public_id=hotel.public_id",
                "hotel_public_id=hotel_public_id",
            ),
        ),
        (f"{PRIORITIES}::test_it_reads_exactly_the_window_and_the_protocols_horizon",),
    ),
]


def screen(name: str, breaks: str, path: str, old: str, new: str, *killers: str) -> Mutation:
    return Mutation(name, "7.13", breaks, one(path, old, new), killers, runner=Runner.VITEST)


STAGE_7_13 = [
    screen(
        "7.13-M1",
        "the answer is rendered as HTML",
        FE_COPILOT + "ExchangeView.tsx",
        '          <p className={styles.answer} data-testid="copilot-answer">\n'
        "            {exchange.answer}\n"
        "          </p>",
        '          <p className={styles.answer} data-testid="copilot-answer" '
        "dangerouslySetInnerHTML={{ __html: exchange.answer }} />",
        f"{ARCH}::contains no dangerouslySetInnerHTML",
        f"{SCREEN}::renders HTML, Markdown and URLs in an answer as the characters they are",
    ),
    screen(
        "7.13-M2",
        "the tool catalogue is rendered",
        FE_COPILOT + "ToolCallList.tsx",
        "          {toolsUsed.map((call, index) => (",
        "          {[...toolsUsed, ...Object.keys(TOOL_LABELS).map((tool) => "
        "({ tool, outcome: 'succeeded' as const }))].map((call, index) => (",
        f"{ARCH}::never renders the tool-label table as a catalogue",
        f"{SCREEN}::shows every tool call the server reported, in order, failures included",
    ),
    screen(
        "7.13-M3",
        "switching hotel keeps the thread",
        FE_COPILOT + "useCopilot.ts",
        "    setOneOff([])\n    setConversation(null)\n",
        "",
        f"{SCREEN}::clears a one-off thread and asks the new hotel next",
    ),
    screen(
        "7.13-M4",
        "there is no in-flight guard",
        FE_COPILOT + "useCopilot.ts",
        "      if (inFlight.current || hotelPublicId === null) {\n        return false\n      }\n"
        "      inFlight.current = true",
        "      if (hotelPublicId === null) {\n        return false\n      }\n      "
        "inFlight.current = true",
        f"{SCREEN}::sends one request for two submits in the same tick",
    ),
    screen(
        "7.13-M5",
        "LLM_DISABLED keeps the input open",
        FE_COPILOT + "useCopilot.ts",
        "          setDisabled(true)",
        "          setDisabled(false)",
        f"{SCREEN}::explains LLM_DISABLED and offers no further input for the visit",
    ),
    screen(
        "7.13-M6",
        "a citation opens the wrong document",
        FE_COPILOT + "useCitationSource.ts",
        "  const documentPublicId = citation.document_public_id",
        "  const documentPublicId = citation.chunk_public_id",
        f"{ARCH}::opens the document the server{TYPOGRAPHIC_APOSTROPHE}s citation names",
    ),
    screen(
        "7.13-M7",
        "a stored turn claims no lookups were made",
        FE_COPILOT + "exchange.ts",
        "    notice: null,\n    toolsUsed: null,",
        "    notice: null,\n    toolsUsed: [],",
        f"{SCREEN}::reopens a stored transcript and says its lookups were not recorded",
    ),
    screen(
        "7.13-M8",
        "the question is kept in sessionStorage",
        FE_COPILOT + "useCopilot.ts",
        "      const trimmed = question.trim()\n",
        "      const trimmed = question.trim()\n      "
        "window.sessionStorage.setItem('copilot.last', trimmed)\n",
        f"{ARCH}::uses no sessionStorage",
        f"{SCREEN}::keeps no question, answer or conversation identifier in the browser",
    ),
    screen(
        "7.13-M9",
        "the server's message is rendered",
        "frontend/src/pages/CopilotPage.tsx",
        "<p className={styles.failureDetail}>{failure.detail}</p>",
        "<p className={styles.failureDetail}>{copilot.failure?.error.message}</p>",
        f"{ARCH}::reads no error message",
    ),
    screen(
        "7.13-M10",
        "stored turns lose the Generated label",
        FE_COPILOT + "ExchangeView.tsx",
        '          <Badge tone="info">{GENERATED_LABEL}</Badge>',
        "          {exchange.recorded === 'live' ? <Badge "
        'tone="info">{GENERATED_LABEL}</Badge> : null}',
        f"{SCREEN}::labels withheld, partial and stored answers too",
    ),
    screen(
        "7.13-M11",
        "a continuation starts a new conversation",
        FE_COPILOT + "useCopilot.ts",
        "      const open = conversation\n      if (open === null) {",
        "      const open = conversation\n      if (open === null || open !== null) {",
        f"{SCREEN}::starts a conversation, then continues it, each turn from the server",
    ),
    screen(
        "7.13-M12",
        "delete happens without confirmation",
        FE_COPILOT + "ConversationList.tsx",
        "            onAskDelete={() => {\n              setConfirming(item.public_id)\n       "
        "     }}",
        "            onAskDelete={() => {\n              void onDelete(item.public_id)\n       "
        "     }}",
        f"{SCREEN}::deletes a conversation only after confirmation, and forgets it",
    ),
    screen(
        "7.13-M13",
        "the provider disclosure is omitted",
        "frontend/src/pages/CopilotPage.tsx",
        "              <p>{PROVIDER_DISCLOSURE}</p>\n",
        "",
        f"{SCREEN}::discloses storage, retention and deletion before the first question",
    ),
    screen(
        "7.13-M14",
        "a role check appears in the browser",
        FE_COPILOT + "ToolCallList.tsx",
        "export function ToolCallList({ toolsUsed }: ToolCallListProps) {",
        "const isManager = (role: string) => role === 'manager'\n"
        "export function ToolCallList({ toolsUsed }: ToolCallListProps) {\n  void isManager",
        f"{ARCH}::contains no a role predicate",
        f"{ARCH}::contains no a role literal",
    ),
]

STAGE_7_14 = [
    Mutation(
        "7.14-M1",
        "7.14",
        "a lag below the horizon is admitted",
        (
            Edit(
                "backend/app/ml/dataset.py",
                "        if lag < horizon_days:\n",
                "        if lag < horizon_days - 1:\n",
            ),
            Edit(
                "backend/app/ml/dataset.py",
                "    if any(lag < horizon_days for lag in lag_days):\n",
                "    if any(lag < horizon_days - 1 for lag in lag_days):\n",
            ),
        ),
        (f"{MULTI_HORIZON}::test_a_lag_shorter_than_the_horizon_is_rejected",),
        note=(
            "The first run disabled one of the dataset contract's two redundant guards and "
            "was not caught; the recorded re-run disabled both, which is what is kept here."
        ),
    ),
    Mutation(
        "7.14-M2",
        "7.14",
        "the baseline uses the wrong lag",
        one(
            HORIZONS,
            "        baseline_lag_days=spec.baseline_lag_days,\n    )\n",
            "        baseline_lag_days=max(spec.lag_days),\n    )\n",
        ),
        (f"{MULTI_HORIZON_REFIT}::test_a_fresh_measurement_reproduces_the_committed_record",),
    ),
    Mutation(
        "7.14-M3",
        "7.14",
        "the on-the-books cutoff leaks a day",
        one(
            OFFLINE_DEMAND,
            "        start = max(booking.arrival_date, booking.booked_date + horizon)",
            "        start = max(booking.arrival_date, booking.booked_date + horizon - _DAY)",
        ),
        (f"{MULTI_HORIZON}::test_a_booking_entered_after_the_cutoff_is_not_on_the_books",),
    ),
    Mutation(
        "7.14-M4",
        "7.14",
        "the 7-day fold floor is applied at every horizon",
        one(
            HORIZONS,
            "        minimum_folds=spec.minimum_folds,\n        required_dataset_version",
            "        minimum_folds=40,\n        required_dataset_version",
        ),
        (f"{MULTI_HORIZON}::test_the_protocol_is_frozen_at_its_pinned_checksum",),
    ),
    Mutation(
        "7.14-M5",
        "7.14",
        "V1 feature admissibility changes",
        one(
            "ml/models.py",
            "def feature_lead_days(name: str, *, dataset_horizon_days: int = 1) -> int | None:",
            "def feature_lead_days(name: str, *, dataset_horizon_days: int = 7) -> int | None:",
        ),
        (f"{MULTI_HORIZON}::test_the_default_dataset_horizon_is_stage_63_for_every_v1_feature",),
    ),
    Mutation(
        "7.14-M6",
        "7.14",
        "the leakage re-check ignores the dataset horizon",
        one(
            "ml/validation.py",
            "        return feature_lead_days(name, dataset_horizon_days=dataset_horizon_days)",
            "        return feature_lead_days(name)",
        ),
        (f"{MULTI_HORIZON_REFIT}::test_a_fresh_measurement_reproduces_the_committed_record",),
    ),
    Mutation(
        "7.14-M7",
        "7.14",
        "a metric is smuggled into acceptance",
        one(
            HORIZONS,
            "        deterministic=deterministic,\n        leakage_checks_passed",
            "        deterministic=deterministic and first.learned_pooled.mae < "
            "first.baseline_pooled.mae,\n"
            "        leakage_checks_passed",
        ),
        (f"{MULTI_HORIZON}::test_the_acceptance_evidence_is_built_from_no_metric",),
    ),
    Mutation(
        "7.14-M8",
        "7.14",
        "the dataset identity is not enforced",
        one(
            HORIZONS,
            '    """Refuse any dataset that is not the one the frozen protocol names, before '
            'measuring."""\n',
            '    """Refuse any dataset that is not the one the frozen protocol names, before '
            'measuring."""\n'
            "    return\n",
        ),
        (f"{MULTI_HORIZON}::test_a_dataset_the_protocol_does_not_name_is_refused",),
    ),
    Mutation(
        "7.14-M9",
        "7.14",
        "the capacity statement is weakened",
        one(
            HORIZONS,
            "Any future serving of these models must declare its own ",
            "Any future serving of these models may declare its own ",
        ),
        (f"{MULTI_HORIZON}::test_the_manifest_states_the_horizon_matching_and_its_limits",),
    ),
    Mutation(
        "7.14-M10",
        "7.14",
        "a cancellation at the cutoff is still counted",
        one(
            OFFLINE_DEMAND,
            "            end = min(end, booking.status_date + horizon)",
            "            end = min(end, booking.status_date + horizon + _DAY)",
        ),
        (f"{MULTI_HORIZON}::test_a_booking_cancelled_by_the_cutoff_is_not_on_the_books",),
    ),
]

STAGE_F2 = [
    Mutation(
        "F2-M1",
        "F2",
        "the purge stops after one batch",
        one(
            CONVERSATION_SERVICE,
            "        if purged < batch_size:\n            break\n",
            "        break\n",
        ),
        (f"{PURGE_JOB}::test_more_than_one_batch_is_processed_to_completion",),
        needs_database=True,
    ),
    Mutation(
        "F2-M2",
        "F2",
        "the retention setting is ignored",
        one(
            CONVERSATION_SERVICE,
            "    retention_days = settings.copilot_conversation_retention_days\n",
            "    retention_days = 30\n",
        ),
        (f"{PURGE_JOB}::test_the_configured_retention_period_decides_what_is_deleted",),
        needs_database=True,
    ),
    Mutation(
        "F2-M3",
        "F2",
        "the global purge is limited to one hotel",
        one(
            CONVERSATION_SERVICE,
            "        purged = retention.purge(limit=batch_size)\n",
            "        purged = retention.purge(limit=batch_size, hotel_id=1)\n",
        ),
        (f"{PURGE_JOB}::test_expired_conversations_at_every_hotel_are_deleted",),
        needs_database=True,
    ),
]

STAGE_I4 = [
    Mutation(
        "I4-M1",
        "I4",
        "a retention day is counted as an hour",
        one(
            INVOCATION_REPOSITORY,
            "        0, 0, 0, 0, retention_days * 24\n    )",
            "        0, 0, 0, 0, retention_days\n    )",
        ),
        (
            f"{INVOCATION_PURGE}::test_a_record_one_day_past_the_period_is_deleted_and_one_day_inside_is_kept",
            f"{INVOCATION_STATIC}::test_a_retention_day_is_counted_as_24_hours_by_the_database",
        ),
        needs_database=True,
    ),
    Mutation(
        "I4-M2",
        "I4",
        "a record exactly at the cutoff is kept",
        one(
            INVOCATION_REPOSITORY,
            "    return LlmInvocation.created_at <= func.now() - func.make_interval(",
            "    return LlmInvocation.created_at < func.now() - func.make_interval(",
        ),
        (f"{INVOCATION_PURGE}::test_the_boundary_is_exact_and_inclusive",),
        needs_database=True,
    ),
    Mutation(
        "I4-M3",
        "I4",
        "one hotel's batch can delete another hotel's records",
        (
            Edit(
                INVOCATION_REPOSITORY,
                "            .where(LlmInvocation.hotel_id == hotel_id, _expired(retention_days))",
                "            .where(_expired(retention_days))",
            ),
            Edit(
                INVOCATION_REPOSITORY,
                "            delete(LlmInvocation).where(\n"
                "                LlmInvocation.hotel_id == hotel_id, LlmInvocation.id.in_(batch)\n"
                "            )",
                "            delete(LlmInvocation).where(LlmInvocation.id.in_(batch))",
            ),
        ),
        (f"{INVOCATION_PURGE}::test_one_hotels_purge_never_touches_another_hotel",),
        needs_database=True,
    ),
    Mutation(
        "I4-M4",
        "I4",
        "a batch deletes the newest expired records first",
        one(
            INVOCATION_REPOSITORY,
            "            .order_by(LlmInvocation.created_at.asc(), LlmInvocation.id.asc())",
            "            .order_by(LlmInvocation.created_at.desc(), LlmInvocation.id.asc())",
        ),
        (f"{INVOCATION_PURGE}::test_a_batch_deletes_the_oldest_expired_records_first",),
        needs_database=True,
    ),
    Mutation(
        "I4-M5",
        "I4",
        "the declared period disagrees with the rule the repository deletes by",
        one(
            INVOCATION_REPOSITORY,
            "str(retention_days * 24), True)",
            "str(retention_days * 24 + 1), True)",
        ),
        (f"{INVOCATION_PURGE}::test_the_boundary_is_exact_and_inclusive",),
        needs_database=True,
    ),
    Mutation(
        "I4-M6",
        "I4",
        "the purge stops after one batch per hotel",
        one(
            INVOCATION_RETENTION,
            "            if purged < batch_size:\n                break\n",
            "            break\n",
        ),
        (
            f"{INVOCATION_PURGE}::test_more_than_one_batch_is_processed_to_completion_at_every_hotel",
        ),
        needs_database=True,
    ),
    Mutation(
        "I4-M7",
        "I4",
        "the purge visits only the first hotel",
        one(
            INVOCATION_RETENTION,
            "    for hotel_id in hotel_ids:\n",
            "    for hotel_id in hotel_ids[:1]:\n",
        ),
        (f"{INVOCATION_PURGE}::test_expired_records_at_every_hotel_and_of_every_user_are_deleted",),
        needs_database=True,
    ),
    Mutation(
        "I4-M8",
        "I4",
        "the retention setting is ignored",
        one(
            INVOCATION_RETENTION,
            "    retention_days = settings.llm_invocation_retention_days\n",
            "    retention_days = 365\n",
        ),
        (f"{INVOCATION_PURGE}::test_the_configured_retention_period_decides_what_is_deleted",),
        needs_database=True,
    ),
    Mutation(
        "I4-M9",
        "I4",
        "batches are committed only at the end, so a failure loses the completed ones",
        one(
            INVOCATION_RETENTION,
            "limit=batch_size)\n                session.commit()\n",
            "limit=batch_size)\n",
        ),
        (
            f"{INVOCATION_PURGE}::test_a_failed_purge_keeps_every_completed_batch_and_a_retry_finishes",
        ),
        needs_database=True,
    ),
    Mutation(
        "I4-M10",
        "I4",
        "the database's one-day floor is removed",
        one(INVOCATION_MIGRATION, "IF retention_hours < 24 THEN", "IF retention_hours < 0 THEN"),
        (
            f"{INVOCATION_PURGE}::test_a_declared_period_shorter_than_a_day_is_refused_whatever_the_record",
        ),
        needs_database=True,
    ),
    Mutation(
        "I4-M11",
        "I4",
        "a declared purge may delete a record inside its period",
        one(
            INVOCATION_MIGRATION,
            "IF OLD.created_at <= now() - make_interval(hours => retention_hours) THEN",
            "IF OLD.created_at <= now() THEN",
        ),
        (f"{INVOCATION_PURGE}::test_a_declared_delete_of_a_record_inside_its_period_is_refused",),
        needs_database=True,
    ),
    Mutation(
        "I4-M12",
        "I4",
        "a declared purge may update a record",
        one(
            INVOCATION_MIGRATION,
            "            IF TG_OP = 'DELETE' THEN",
            "            IF TG_OP IN ('DELETE', 'UPDATE') THEN",
        ),
        (f"{INVOCATION_PURGE}::test_an_update_is_refused_even_inside_a_declared_purge",),
        needs_database=True,
    ),
    Mutation(
        "I4-M13",
        "I4",
        "the accounting retention may be shorter than the conversation retention",
        one(
            CONFIG,
            "retention_days < self.copilot_conversation_retention_days:",
            "retention_days < 0:",
        ),
        (
            f"{INVOCATION_STATIC}::test_the_retention_may_not_be_shorter_than_the_conversation_retention",
        ),
    ),
    Mutation(
        "I4-M14",
        "I4",
        "the default period is not the approved one",
        one(
            CONFIG,
            "    llm_invocation_retention_days: int = Field(default=365, ge=1, le=3650)",
            "    llm_invocation_retention_days: int = Field(default=730, ge=1, le=3650)",
        ),
        (f"{INVOCATION_STATIC}::test_the_default_retention_is_the_approved_365_days",),
    ),
]

STAGE_F1 = [
    Mutation(
        "F1-M1",
        "F1",
        "booking days are bucketed in the session's time zone again",
        (
            Edit(
                ANALYTICS_REPOSITORY,
                "            _local_day(Booking.booked_at, zone),\n"
                "            Booking,\n"
                "            Booking.hotel_id == hotel_id,\n"
                "            _within_local_days(Booking.booked_at, date_from, date_to, zone),\n",
                "            func.date(Booking.booked_at),\n"
                "            Booking,\n"
                "            Booking.hotel_id == hotel_id,\n"
                "            func.date(Booking.booked_at) >= date_from,\n"
                "            func.date(Booking.booked_at) <= date_to,\n",
            ),
            Edit(
                ANALYTICS_REPOSITORY,
                "                    _within_local_days(Booking.booked_at,"
                " date_from, date_to, zone),\n",
                "                    func.date(Booking.booked_at) >= date_from,\n"
                "                    func.date(Booking.booked_at) <= date_to,\n",
            ),
        ),
        (
            f"{BUCKETING}::test_athens_bookings_fall_on_the_hotels_own_calendar_day",
            f"{BUCKETING}::test_a_one_day_range_is_exactly_that_local_day",
            f"{BUSINESS_DAY_GUARD}::test_no_repository_or_service_buckets_a_booking_instant_in_the_session_zone",
        ),
        needs_database=True,
    ),
    Mutation(
        "F1-M2",
        "F1",
        "cancellation days are bucketed in the session's time zone again",
        (
            Edit(
                ANALYTICS_REPOSITORY,
                "            _local_day(Booking.cancelled_at, zone),\n"
                "            Booking,\n"
                "            Booking.hotel_id == hotel_id,\n"
                "            _within_local_days(Booking.cancelled_at, date_from, date_to, zone),\n",
                "            func.date(Booking.cancelled_at),\n"
                "            Booking,\n"
                "            Booking.hotel_id == hotel_id,\n"
                "            func.date(Booking.cancelled_at) >= date_from,\n"
                "            func.date(Booking.cancelled_at) <= date_to,\n",
            ),
            Edit(
                ANALYTICS_REPOSITORY,
                "        return self._count(\n"
                "            Booking,\n"
                "            Booking.hotel_id == hotel_id,\n"
                "            _within_local_days(Booking.cancelled_at, date_from, date_to, zone),\n",
                "        return self._count(\n"
                "            Booking,\n"
                "            Booking.hotel_id == hotel_id,\n"
                "            func.date(Booking.cancelled_at) >= date_from,\n"
                "            func.date(Booking.cancelled_at) <= date_to,\n",
            ),
        ),
        (
            f"{BUCKETING}::test_cancellations_fall_on_the_hotels_own_calendar_day",
            f"{BUCKETING}::test_a_one_day_range_is_exactly_that_local_day",
            f"{BUSINESS_DAY_GUARD}::test_no_repository_or_service_buckets_a_booking_instant_in_the_session_zone",
        ),
        needs_database=True,
    ),
    Mutation(
        "F1-M3",
        "F1",
        "the hotel's timezone is ignored: every hotel is bucketed in UTC",
        one(
            ANALYTICS_REPOSITORY,
            "            return declared\n        return FALLBACK_TIMEZONE\n",
            "            return FALLBACK_TIMEZONE\n        return FALLBACK_TIMEZONE\n",
        ),
        (
            f"{BUCKETING}::test_athens_bookings_fall_on_the_hotels_own_calendar_day",
            f"{BUCKETING}::test_the_effective_timezone_is_a_postgresql_named_zone_or_utc",
        ),
        needs_database=True,
    ),
    Mutation(
        "F1-M4",
        "F1",
        "Athens is read as a fixed UTC+2 offset instead of its IANA zone",
        one(
            ANALYTICS_REPOSITORY,
            "            return declared\n        return FALLBACK_TIMEZONE\n",
            '            return "Etc/GMT-2" if declared == "Europe/Athens" else declared\n'
            "        return FALLBACK_TIMEZONE\n",
        ),
        (
            f"{BUCKETING}::test_the_day_clocks_go_forward_is_23_hours_long",
            f"{BUCKETING}::test_the_day_clocks_go_back_is_25_hours_long",
        ),
        needs_database=True,
        note="Etc/GMT-2 is UTC+2 all year: right in winter, an hour wrong in summer.",
    ),
    Mutation(
        "F1-M5",
        "F1",
        "a range's upper bound is inclusive of the next local midnight",
        one(
            ANALYTICS_REPOSITORY,
            "        instant < _local_midnight(date_to + dt.timedelta(days=1), zone),",
            "        instant <= _local_midnight(date_to + dt.timedelta(days=1), zone),",
        ),
        (f"{BUCKETING}::test_a_one_day_range_is_exactly_that_local_day",),
        needs_database=True,
    ),
    Mutation(
        "F1-M6",
        "F1",
        "grouping is hotel-local but the range filter is session-local",
        one(
            ANALYTICS_REPOSITORY,
            "    return and_(\n"
            "        instant >= _local_midnight(date_from, zone),\n"
            "        instant < _local_midnight(date_to + dt.timedelta(days=1), zone),\n"
            "    )",
            "    return and_(func.date(instant) >= date_from, func.date(instant) <= date_to)",
        ),
        (
            f"{BUCKETING}::test_a_one_day_range_is_exactly_that_local_day",
            f"{BUCKETING}::test_the_overview_and_the_daily_series_count_the_same_local_days",
        ),
        needs_database=True,
    ),
    Mutation(
        "F1-M7",
        "F1",
        "the intelligence booking series is not given the hotel's timezone",
        one(
            INTELLIGENCE_SERVICE,
            "            zone=self._repository.business_timezone(hotel_id),",
            '            zone="UTC",',
        ),
        (
            f"{BUCKETING}::test_the_booking_trend_reads_the_hotels_calendar_days",
            f"{BUCKETING}::test_a_booking_volume_anomaly_is_dated_on_the_hotels_calendar_day",
        ),
        needs_database=True,
    ),
    Mutation(
        "F1-M8",
        "F1",
        "an unrecognised timezone is used as it is, and fails",
        one(
            ANALYTICS_REPOSITORY,
            "        if declared in AnalyticsRepository._named_timezones:\n"
            "            return declared\n"
            "        return FALLBACK_TIMEZONE\n",
            "        return declared\n",
        ),
        (
            f"{BUCKETING}::test_an_unrecognised_timezone_buckets_in_utc_and_does_not_fail",
            f"{BUCKETING}::test_the_effective_timezone_is_a_postgresql_named_zone_or_utc",
            f"{BUCKETING}::test_the_api_reports_hotel_local_days_whatever_the_server_session_zone",
        ),
        needs_database=True,
    ),
]


def adr(name: str, breaks: str, old: str, new: str, *titles: str) -> Mutation:
    """A dashboard ADR-caption mutation (F2): one edit to the page, Vitest killers by title."""
    return Mutation(
        name,
        "F2-ADR",
        breaks,
        one(DASHBOARD_PAGE, old, new),
        tuple(f"{DASHBOARD_TESTS}::{title}" for title in titles),
        runner=Runner.VITEST,
    )


ADR_SEVERAL = "names the currency, not the all-currency count, when several earned room revenue"
ADR_LEDGER = "keeps the count when the second currency is only in the ledger"
ADR_MISSING = (
    "says EUR room revenue is missing, not that no nights were sold, when only USD earned it"
)

STAGE_F2_ADR = [
    adr(
        "F2-ADR-M1",
        "several room currencies quote the all-currency night count again",
        "    return `Average daily rate over ${currency} nights sold`",
        "    return `Average daily rate over ${formatCount(overview.occupancy.room_nights_sold)} "
        "nights sold`",
        ADR_SEVERAL,
    ),
    adr(
        "F2-ADR-M2",
        "several room currencies show the all-currency count beside the currency",
        "    return `Average daily rate over ${currency} nights sold`",
        "    return `Average daily rate over ${formatCount(overview.occupancy.room_nights_sold)} "
        "${currency} nights sold`",
        ADR_SEVERAL,
    ),
    adr(
        "F2-ADR-M3",
        "the several-currency caption loses its currency",
        "    return `Average daily rate over ${currency} nights sold`",
        "    return `Average daily rate over nights sold`",
        ADR_SEVERAL,
    ),
    adr(
        "F2-ADR-M4",
        "a missing headline bucket claims again that no room nights were sold",
        "    return `No room revenue in ${currency} was recorded in this period, so ADR was not "
        "reported.`",
        "    return 'No room nights were sold in this period, so the average rate is undefined.'",
        ADR_MISSING,
    ),
    adr(
        "F2-ADR-M5",
        "a currency only in the ledger switches the caption to several-currency wording",
        "  if (currenciesIn(overview.room_revenue).length > 1) {",
        "  if (overview.is_multi_currency) {",
        ADR_LEDGER,
    ),
    adr(
        "F2-ADR-M6",
        "a missing headline bucket falls through to the generic no-nights reason",
        "  if (room === null && overview.occupancy.room_nights_sold > 0) {",
        "  if (room === null && overview.occupancy.room_nights_sold < 0) {",
        ADR_MISSING,
    ),
]


def runbook(name: str, breaks: str, path: str, old: str, new: str, *tests: str) -> Mutation:
    """A runbook drift mutation (M1): one edit, killed by the runbook schema-facts test."""
    return Mutation(
        name, "M1", breaks, one(path, old, new), tuple(f"{RUNBOOK_FACTS}::{t}" for t in tests)
    )


STALE = "`0011_demand_prediction_public_id`"
CURRENT = "`0020_guest_email_rules`"
REVISIONS_IN_BACKUP = "test_every_revision_a_runbook_quotes_is_the_current_head[backup-restore]"

STAGE_M1 = [
    runbook(
        "M1-M1",
        "the backup stop condition names an old revision again",
        BACKUP_RUNBOOK,
        f"revision is not {CURRENT}, stop",
        f"revision is not {STALE}, stop",
        REVISIONS_IN_BACKUP,
    ),
    runbook(
        "M1-M2",
        "the backup prerequisite names an old revision again",
        BACKUP_RUNBOOK,
        f"so the schema is at {CURRENT}.",
        f"so the schema is at {STALE}.",
        REVISIONS_IN_BACKUP,
    ),
    runbook(
        "M1-M3",
        "the restore check expects an old revision again",
        BACKUP_RUNBOOK,
        f"Expect {CURRENT} —",
        f"Expect {STALE} —",
        REVISIONS_IN_BACKUP,
    ),
    runbook(
        "M1-M4",
        "the restore check forgets alembic_version in its base-table count",
        BACKUP_RUNBOOK,
        "Expect **29** — the 28 application tables",
        "Expect **28** — the 28 application tables",
        "test_the_restore_check_expects_every_application_table_plus_alembic_version",
    ),
    runbook(
        "M1-M5",
        "the first-run prerequisite names an old revision again",
        BOOTSTRAP_RUNBOOK,
        f"Alembic reports revision **{CURRENT}**",
        f"Alembic reports revision **{STALE}**",
        "test_every_revision_a_runbook_quotes_is_the_current_head[first-run-bootstrap]",
    ),
    runbook(
        "M1-M6",
        "the backup runbook states a migration count again",
        BACKUP_RUNBOOK,
        "the schema is managed by Alembic and rebuilt from the migrations",
        "the schema is seventeen Alembic migrations, rebuilt from the migrations",
        "test_the_backup_runbook_states_no_migration_count",
    ),
    runbook(
        "M1-M7",
        "the revision extraction matches nothing, so the runbook check asserts nothing",
        RUNBOOK_FACTS,
        'QUOTED_REVISION = re.compile(r"`(\\d{4}_[a-z][a-z0-9_]*)`")',
        'QUOTED_REVISION = re.compile(r"`(\\d{5}_[a-z][a-z0-9_]*)`")',
        "test_the_revision_extraction_finds_a_stale_revision",
    ),
    runbook(
        "M1-M8",
        "the table extraction matches nothing, so the restore count is never compared",
        RUNBOOK_FACTS,
        '    r"Expect \\*\\*(\\d+)\\*\\* — the (\\d+) application tables plus `alembic_version`"',
        '    r"Expected \\*\\*(\\d+)\\*\\* — the (\\d+) application tables plus `alembic_version`"',
        "test_the_table_extraction_finds_a_stale_count",
    ),
]


def current_state(name: str, breaks: str, path: str, old: str, new: str, test: str) -> Mutation:
    """A current-state documentation mutation (M2), killed by the stale-claim guard."""
    return Mutation(name, "M2", breaks, one(path, old, new), (f"{CURRENT_STATE_DOCS}::{test}",))


STALE_ABSENT = "test_a_corrected_stale_claim_is_absent"

STAGE_M2 = [
    current_state(
        "M2-M1",
        "the copilot screen doc says again that no route reports the switch",
        COPILOT_FRONTEND_DOC,
        "- **`LLM_DISABLED` is known before asking, except when the check fails.**",
        "- **`LLM_DISABLED` is learned by asking.** No route says in advance whether the copilot is"
        " on.",
        STALE_ABSENT,
    ),
    current_state(
        "M2-M2",
        "the README says again the served model has no front-end surface",
        "README.md",
        "and shown on the Analytics page\n",
        "and with no front-end surface\n",
        STALE_ABSENT,
    ),
    current_state(
        "M2-M3",
        "ml-serving says again that the endpoints have no frontend surface",
        ML_SERVING_DOC,
        "7. **~~No frontend surface.~~ Resolved.**",
        "7. **No frontend surface.**",
        STALE_ABSENT,
    ),
    current_state(
        "M2-M4",
        "the copilot screen doc no longer names the capability the API reports",
        COPILOT_FRONTEND_DOC,
        "`GET /api/v1/` reports\n  `copilot_enabled`,",
        "`GET /api/v1/` reports\n  the switch,",
        "test_the_copilot_screen_documents_the_capability_the_api_reports",
    ),
    current_state(
        "M2-M5",
        "ml-serving no longer names the frontend service that calls both routes",
        ML_SERVING_DOC,
        "`frontend/src/services/ml/mlService.ts`. The copilot",
        "the frontend's ML service. The copilot",
        "test_the_served_model_s_frontend_callers_are_the_ones_the_frontend_has",
    ),
    current_state(
        "M2-M6",
        "the matcher no longer ignores line wrapping, so a re-wrapped claim would slip through",
        CURRENT_STATE_DOCS,
        '    return re.sub(r"\\s+", " ", text)',
        "    return text",
        "test_the_matcher_finds_each_claim_in_its_original_wrapping",
    ),
    current_state(
        "M2-M7",
        "the frontend cross-check reads a service that does not call the ML routes",
        CURRENT_STATE_DOCS,
        '"frontend" / "src" / "services" / "ml" / "mlService.ts"',
        '"frontend" / "src" / "services" / "analytics" / "analyticsService.ts"',
        "test_the_served_model_s_frontend_callers_are_the_ones_the_frontend_has",
    ),
    current_state(
        "M2-M8",
        "the settings docstring says again that the secret is unread",
        "backend/app/core/config.py",
        "never built. ``secret_key`` signs and verifies authentication tokens\n",
        "never built. ``secret_key`` remains declared but unread -- there is no authentication"
        " yet.\n",
        STALE_ABSENT,
    ),
]


def comp(name: str, breaks: str, path: str, old: str, new: str, killer: str) -> Mutation:
    """A complimentary-nights ADR-reason mutation (F2 residual): Vitest killer by file::title."""
    return Mutation(name, "F2-COMP", breaks, one(path, old, new), (killer,), runner=Runner.VITEST)


COMP_DASHBOARD_SAYS = (
    f"{DASHBOARD_TESTS}::says the headline currency sold no nights, not that none were sold, "
    "when another did"
)
COMP_DASHBOARD_KEEPS = (
    f"{DASHBOARD_TESTS}::still says no nights were sold when every night was complimentary"
)
COMP_ANALYTICS_SAYS = (
    f"{ANALYTICS_TESTS}::says the reporting currency sold no nights, not that none were sold, "
    "when another did"
)
COMP_ANALYTICS_KEEPS = (
    f"{ANALYTICS_TESTS}::still says no nights were sold when every night was complimentary"
)

STAGE_F2_COMP = [
    comp(
        "F2-COMP-M1",
        "the dashboard says no room nights were sold although another currency sold some",
        DASHBOARD_PAGE,
        "  if (room !== null && room.adr === null && overview.occupancy.room_nights_sold > 0) {",
        "  if (room !== null && room.adr === null && overview.occupancy.room_nights_sold < 0) {",
        COMP_DASHBOARD_SAYS,
    ),
    comp(
        "F2-COMP-M2",
        "the dashboard names the currency even when the hotel sold nothing at all",
        DASHBOARD_PAGE,
        "  if (room !== null && room.adr === null && overview.occupancy.room_nights_sold > 0) {",
        "  if (room !== null && room.adr === null) {",
        COMP_DASHBOARD_KEEPS,
    ),
    comp(
        "F2-COMP-M3",
        "the dashboard reason no longer names the currency",
        DASHBOARD_PAGE,
        "`No room nights in ${currency} were sold",
        "`No room nights in this currency were sold",
        COMP_DASHBOARD_SAYS,
    ),
    comp(
        "F2-COMP-M4",
        "the analytics page says no room nights were sold although another currency sold some",
        ANALYTICS_PAGE,
        "  if (room !== null && room.adr === null && roomNightsSold > 0) {",
        "  if (room !== null && room.adr === null && roomNightsSold < 0) {",
        COMP_ANALYTICS_SAYS,
    ),
    comp(
        "F2-COMP-M5",
        "the analytics page names the currency even when the hotel sold nothing at all",
        ANALYTICS_PAGE,
        "  if (room !== null && room.adr === null && roomNightsSold > 0) {",
        "  if (room !== null && room.adr === null) {",
        COMP_ANALYTICS_KEEPS,
    ),
    comp(
        "F2-COMP-M6",
        "the analytics reason no longer names the currency",
        ANALYTICS_PAGE,
        "`No room nights in ${room.currency} were sold",
        "`No room nights in this currency were sold",
        COMP_ANALYTICS_SAYS,
    ),
]


def points(name: str, breaks: str, path: str, old: str, new: str, killer: str) -> Mutation:
    """An occupancy-change mutation (F3): one edit, a Vitest killer by file::title."""
    return Mutation(name, "F3", breaks, one(path, old, new), (killer,), runner=Runner.VITEST)


DELTA_FORMAT = "frontend/src/features/dashboard/format.ts"
PERIOD_TESTS = "src/features/dashboard/period.test.ts"
F3_DERIVES = f"{DASHBOARD_TESTS}::derives the change from two authoritative windows"

STAGE_F3 = [
    points(
        "F3-M1",
        "occupancy is compared as a relative change again",
        DASHBOARD_PAGE,
        "          delta={computePointsDelta(\n            overview.occupancy.occupancy_rate,",
        "          delta={computeDelta(\n            overview.occupancy.occupancy_rate,",
        F3_DERIVES,
    ),
    points(
        "F3-M2",
        "the points change forgets to scale the rates to percent",
        DELTA_FORMAT,
        "  const change = (now - before) * 100\n",
        "  const change = now - before\n",
        f"{PERIOD_TESTS}::is the difference of the two rates, times one hundred",
    ),
    points(
        "F3-M3",
        "ADR, an amount, is compared in points",
        DASHBOARD_PAGE,
        "delta={computeDelta(room?.adr ?? null, previousRoom?.adr ?? null)}",
        "delta={computePointsDelta(room?.adr ?? null, previousRoom?.adr ?? null)}",
        F3_DERIVES,
    ),
    points(
        "F3-M4",
        "a points change is printed as a percent",
        DELTA_FORMAT,
        "    return `${signedOneDecimal(delta.change, locale)} pts`",
        "    return `${signedOneDecimal(delta.change, locale)}%`",
        f"{PERIOD_TESTS}::is printed as points and spoken as percentage points",
    ),
    points(
        "F3-M5",
        "the flat threshold is the relative one, applied to points",
        DELTA_FORMAT,
        "  if (Math.abs(change) < 0.05) {",
        "  if (Math.abs(change) < 0.0005) {",
        f"{PERIOD_TESTS}::calls a change below 0.05 points flat, "
        "and one of 0.05 points a direction",
    ),
    points(
        "F3-M6",
        "a previous occupancy of zero is refused, as a relative change would be",
        DELTA_FORMAT,
        "  if (!Number.isFinite(now) || !Number.isFinite(before)) {",
        "  if (!Number.isFinite(now) || !Number.isFinite(before) || before === 0) {",
        f"{PERIOD_TESTS}::is defined against a previous rate of zero",
    ),
    points(
        "F3-M7",
        "a screen reader hears the abbreviation instead of percentage points",
        DELTA_FORMAT,
        "    return `${signedOneDecimal(delta.change, locale)} percentage points`",
        "    return `${signedOneDecimal(delta.change, locale)} pts`",
        f"{DASHBOARD_TESTS}::reads the occupancy change aloud as percentage points",
    ),
    points(
        "F3-M8",
        "the page no longer says which changes are points and which are relative",
        DASHBOARD_PAGE,
        "this one starts. Occupancy changes\n",
        "this one starts. Changes\n",
        f"{DASHBOARD_TESTS}::says which changes are points and which are relative",
    ),
]


STAY_FLOW = "tests/integration/test_stay_flow_statuses.py"
STAY_FLOW_CONTRACT = "tests/backend/test_stay_flow_contract.py"
STATUS_FILTER = "            Booking.status.in_(OCCUPANCY_STATUSES),\n        )\n\n"


def stay_flow(name: str, breaks: str, after: str, killer: str, new_filter: str = "") -> Mutation:
    """Drop (or replace) the status filter in the stay-flow method followed by *after* (F4)."""
    return Mutation(
        name,
        "F4",
        breaks,
        one(ANALYTICS_REPOSITORY, STATUS_FILTER + after, new_filter + "        )\n\n" + after),
        (f"{STAY_FLOW}::{killer}",),
        needs_database=True,
    )


STAGE_F4 = [
    stay_flow(
        "F4-M1",
        "arrivals count every status again",
        "    def departure_count(",
        "test_only_bookings_that_occupy_a_room_arrive_and_depart",
    ),
    stay_flow(
        "F4-M2",
        "departures count every status again",
        "    def cancellation_count(",
        "test_only_bookings_that_occupy_a_room_arrive_and_depart",
    ),
    stay_flow(
        "F4-M3",
        "the daily arrival series counts every status again",
        "    def departures_by_day(",
        "test_the_daily_series_counts_the_same_bookings",
    ),
    stay_flow(
        "F4-M4",
        "the daily departure series counts every status again",
        "    def cancellations_by_day(",
        "test_the_daily_series_counts_the_same_bookings",
    ),
    stay_flow(
        "F4-M5",
        "arrivals use the inventory-holding set, which forgets completed stays",
        "    def departure_count(",
        "test_each_counted_status_is_an_arrival_and_a_departure",
        new_filter='            Booking.status.in_(("confirmed", "checked_in")),\n',
    ),
    Mutation(
        "F4-M6",
        "F4",
        "the dashboard calls every overlapping booking 'staying' again",
        one(
            DASHBOARD_PAGE,
            "<dt>Bookings overlapping this period (any status)</dt>",
            "<dt>Bookings staying</dt>",
        ),
        (
            f"{DASHBOARD_TESTS}::says which bookings arrive and depart, and that overlap counts "
            "every status",
        ),
        runner=Runner.VITEST,
    ),
    Mutation(
        "F4-M7",
        "F4",
        "the analytics page says arrivals were recorded, though confirmed ones are only expected",
        one(
            ANALYTICS_PAGE,
            'emptyMessage="No arrivals were expected or recorded in this period."',
            'emptyMessage="No arrivals were recorded in this period."',
        ),
        (
            f"{ANALYTICS_TESTS}::says no arrival was expected or recorded, not merely recorded, "
            "for an empty series",
        ),
        runner=Runner.VITEST,
    ),
    Mutation(
        "F4-M8",
        "F4",
        "the copilot's daily-series tool no longer tells the model who is counted",
        one(
            "backend/app/copilot/tools/daily_series.py",
            ' Arrivals and departures "\n        "count confirmed, checked-in and checked-out '
            'bookings only."',
            '"',
        ),
        (f"{STAY_FLOW_CONTRACT}::test_each_copilot_tool_tells_the_model_who_is_counted",),
    ),
]

LIVE_CONTRACT = f"{CONTRACT}::test_the_frontend_types_are_compatible_with_the_live_backend_schema"

STAGE_F16 = [
    Mutation(
        "F16-M1",
        "F16",
        "a citation's version becomes a string",
        one(
            "backend/app/schemas/copilot.py",
            '    version: int = Field(description="That version\'s number.")',
            '    version: str = Field(description="That version\'s number.")',
        ),
        (LIVE_CONTRACT,),
    ),
    Mutation(
        "F16-M2",
        "F16",
        "the answer loses its notice field",
        one(
            "backend/app/schemas/copilot.py",
            "    notice: str | None = Field(",
            "    notice_text: str | None = Field(",
        ),
        (LIVE_CONTRACT,),
    ),
    Mutation(
        "F16-M3",
        "F16",
        "a new stop reason appears",
        one(
            "backend/app/schemas/copilot.py",
            '    "ungrounded_figures",\n]',
            '    "ungrounded_figures",\n    "timed_out",\n]',
        ),
        (LIVE_CONTRACT,),
    ),
    Mutation(
        "F16-M4",
        "F16",
        "a citation's document id is renamed",
        one(
            "backend/app/schemas/copilot.py",
            "    document_public_id: uuid.UUID = Field(",
            "    document_id: uuid.UUID = Field(",
        ),
        (LIVE_CONTRACT,),
    ),
    Mutation(
        "F16-M5",
        "F16",
        "a transcript's turns change shape",
        one(
            "backend/app/schemas/copilot_conversation.py",
            "    turns: list[StoredTurn]",
            "    turns: list[ConversationSummary]",
        ),
        (LIVE_CONTRACT,),
    ),
    Mutation(
        "F16-M6",
        "F16",
        "a turn's tools_used becomes nullable",
        one(
            "backend/app/schemas/copilot_conversation.py",
            "    tools_used: list[CopilotToolUse]\n",
            "    tools_used: list[CopilotToolUse] | None\n",
        ),
        (LIVE_CONTRACT,),
    ),
    Mutation(
        "F16-M7",
        "F16",
        "a labelled tool is renamed",
        one(
            "backend/app/copilot/tools/priorities.py",
            'name="get_hotel_priorities"',
            'name="get_attention_list"',
        ),
        (f"{CONTRACT}::test_every_labelled_tool_is_still_registered",),
    ),
    Mutation(
        "F16-M8",
        "F16",
        "the CONVERSATION_FULL code is renamed",
        one(
            CONVERSATION_SERVICE,
            'CONVERSATION_FULL = "CONVERSATION_FULL"',
            'CONVERSATION_FULL = "CONVERSATION_LIMIT"',
        ),
        (f"{CONTRACT}::test_every_error_code_the_copilot_screen_handles_is_one_the_backend_sends",),
    ),
    Mutation(
        "F16-M9",
        "F16",
        "the frontend invents a field",
        one(
            "frontend/src/types/copilot.ts",
            "  readonly version: number\n}",
            "  readonly version: number\n  readonly page: number\n}",
        ),
        (LIVE_CONTRACT,),
    ),
    Mutation(
        "F16-M10",
        "F16",
        "the frontend drops a document status",
        one(
            "frontend/src/types/knowledge.ts",
            "'active' | 'superseded' | 'withdrawn'",
            "'active' | 'withdrawn'",
        ),
        (LIVE_CONTRACT,),
    ),
    Mutation(
        "F16-M11",
        "F16",
        "a frontend service call reads the wrong type",
        one(
            "frontend/src/services/copilot/copilotService.ts",
            "api.get<ConversationTranscript>(",
            "api.get<ConversationSummary>(",
        ),
        (f"{CONTRACT}::test_each_binding_is_the_call_the_frontend_service_makes",),
    ),
    Mutation(
        "F16-M12",
        "F16",
        "the frontend types use a construct the checker cannot read",
        one(
            "frontend/src/types/copilot.ts",
            "  readonly turns_remaining: number\n}",
            "  readonly turns_remaining: number | undefined\n}",
        ),
        (LIVE_CONTRACT,),
    ),
]

OPENAPI_HOOK = "backend/app/api/openapi.py"
OPENAPI_AUTHORIZATION = "tests/backend/test_openapi_authorization.py"
SECURED_401 = f"{OPENAPI_AUTHORIZATION}::test_every_authenticated_operation_declares_401"
GATED_403 = f"{OPENAPI_AUTHORIZATION}::test_every_route_level_gate_declares_403"
ADDS_401 = '                if operation.get("security") and "401" not in responses:\n'
ADDS_403 = '                if reason is not None and "403" not in responses:\n'


def hook(name: str, breaks: str, old: str, new: str, killer: str) -> Mutation:
    """An edit to the OpenAPI hook of G1, caught by the named test in its test file."""
    return Mutation(name, "G1", breaks, one(OPENAPI_HOOK, old, new), (killer,))


STAGE_G1 = [
    hook(
        "G1-M1",
        "the hook stops declaring 401 on authenticated operations",
        ADDS_401,
        '                if operation.get("security") and "401" in responses:\n',
        SECURED_401,
    ),
    hook(
        "G1-M2",
        "401 is declared on operations that take no bearer token",
        ADDS_401,
        '                if "401" not in responses:\n',
        f"{OPENAPI_AUTHORIZATION}::"
        "test_an_operation_without_a_bearer_token_declares_no_401_but_a_failed_sign_in",
    ),
    hook(
        "G1-M3",
        "a hand-written 401 is overwritten by the generic one",
        ADDS_401,
        '                if operation.get("security"):\n',
        f"{OPENAPI_AUTHORIZATION}::test_a_hand_written_401_is_kept_as_written",
    ),
    hook(
        "G1-M4",
        "the platform-administrator gate is no longer seen, so its 403 goes undeclared",
        "        if dependency.call is require_platform_admin:\n"
        "            return PLATFORM_FORBIDDEN_DESCRIPTION\n",
        "",
        GATED_403,
    ),
    hook(
        "G1-M5",
        "a missing platform grant is described as a hotel role that is too low",
        "            return PLATFORM_FORBIDDEN_DESCRIPTION\n",
        "            return ROLE_FORBIDDEN_DESCRIPTION\n",
        f"{OPENAPI_AUTHORIZATION}::test_the_generated_403_names_the_authority_that_is_missing",
    ),
    hook(
        "G1-M6",
        "a viewer-level route claims a 403 it can never answer",
        "        if role is not None and role is not HotelRole.VIEWER:\n",
        "        if role is not None:\n",
        f"{OPENAPI_AUTHORIZATION}::test_a_403_is_claimed_only_where_it_can_be_answered",
    ),
    hook(
        "G1-M7",
        "a hand-written 403 is overwritten by the generic one",
        ADDS_403,
        "                if reason is not None:\n",
        f"{OPENAPI_AUTHORIZATION}::test_a_hand_written_403_is_kept_as_written",
    ),
    Mutation(
        "G1-M8",
        "G1",
        "require_role no longer marks its role, so no route-level role gate gets a 403",
        one(
            "backend/app/api/deps.py",
            "    dependency.minimum_role = required  # type: ignore[attr-defined]\n",
            "",
        ),
        (GATED_403,),
    ),
    Mutation(
        "G1-M9",
        "G1",
        "updating a hotel no longer declares the owner-only 403 its service answers",
        one(
            "backend/app/api/v1/endpoints/hotels.py",
            '    summary="Partially update a hotel",\n'
            "    responses={\n"
            "        **NOT_FOUND_RESPONSE,\n"
            "        **OWNER_REQUIRED_RESPONSE,\n",
            '    summary="Partially update a hotel",\n'
            "    responses={\n"
            "        **NOT_FOUND_RESPONSE,\n",
        ),
        (
            f"{OPENAPI_AUTHORIZATION}::"
            "test_a_role_checked_inside_a_service_is_declared_on_its_route",
        ),
    ),
    Mutation(
        "G1-M10",
        "G1",
        "the application no longer installs the hook",
        one("backend/app/main.py", "    document_authorization_errors(app)\n", ""),
        (SECURED_401,),
    ),
]


BOOKING_SERVICE = "backend/app/services/booking.py"
INACTIVE_ROOMS = "tests/integration/test_inactive_rooms.py"
ROOMS_TESTS = "src/features/rooms/rooms.test.tsx"
PROPERTY_TESTS = "src/features/property/property.test.tsx"
FOR_SALE_BY_NUMBER = (
    "        self._require_for_sale([rooms[room_input.room_number].id for room_input in "
    "payload.rooms])\n"
)
HOLDS_AFTER = "                        BookingRoom.check_out_date > day,\n"
HOLDS_AFTER_OR_ON = "                        BookingRoom.check_out_date >= day,\n"
HOLDING = "                        BookingRoom.booking_status.in_(INVENTORY_HOLDING_STATUSES),\n"


def inactive(name: str, breaks: str, edits: tuple[Edit, ...], killer: str) -> Mutation:
    """An H1 mutation whose killer is an integration test in test_inactive_rooms.py."""
    return Mutation(
        name, "H1", breaks, edits, (f"{INACTIVE_ROOMS}::{killer}",), needs_database=True
    )


STAGE_H1 = [
    inactive(
        "H1-M1",
        "a new booking can name an inactive room again",
        one(
            BOOKING_SERVICE,
            FOR_SALE_BY_NUMBER + "\n        # Priced before the write",
            "\n        # Priced before the write",
        ),
        "test_a_booking_cannot_be_created_on_an_inactive_room",
    ),
    inactive(
        "H1-M2",
        "a stay can be moved onto an inactive room again",
        one(
            BOOKING_SERVICE,
            FOR_SALE_BY_NUMBER + "\n        # Computed BEFORE anything is written",
            "\n        # Computed BEFORE anything is written",
        ),
        "test_a_stay_cannot_be_moved_onto_an_inactive_room",
    ),
    inactive(
        "H1-M3",
        "a pending booking on a retired room can be confirmed, putting the room back on sale",
        one(
            BOOKING_SERVICE,
            "                self._require_for_sale([allocation.room_id for allocation in "
            "booking.booking_rooms])\n",
            "                pass\n",
        ),
        "test_a_pending_booking_on_a_retired_room_cannot_be_confirmed",
    ),
    inactive(
        "H1-M4",
        "a stay on an inactive room can be extended again",
        one(
            BOOKING_SERVICE,
            "        self._require_for_sale([allocation.room_id for allocation in allocations])\n",
            "",
        ),
        "test_a_stay_on_an_inactive_room_cannot_be_extended",
    ),
    inactive(
        "H1-M5",
        "a room of an inactive type is for sale",
        one(
            BOOKING_SERVICE,
            "            number, room_active, type_active = for_sale[room_id]\n",
            "            number, room_active, _ = for_sale[room_id]\n"
            "            type_active = True\n",
        ),
        "test_a_booking_cannot_be_created_on_a_room_of_an_inactive_type",
    ),
    inactive(
        "H1-M6",
        "a room with a stay ahead can be deactivated",
        one(
            "backend/app/services/room.py",
            "            room = self._require_retirable(hotel, room)\n",
            "            pass\n",
        ),
        "test_a_room_with_a_stay_ahead_cannot_be_deactivated",
    ),
    inactive(
        "H1-M7",
        "a room type with a stay ahead on one of its rooms can be deactivated",
        one(
            "backend/app/services/room_type.py",
            "            room_type = self._require_retirable(hotel, room_type)\n",
            "            pass\n",
        ),
        "test_a_room_type_with_a_stay_ahead_on_any_room_cannot_be_deactivated",
    ),
    inactive(
        "H1-M8",
        "today is UTC's, not the hotel's",
        one(
            "backend/app/services/business_day.py",
            "        zone = ZoneInfo(hotel.timezone)\n",
            '        zone = ZoneInfo("UTC")\n',
        ),
        "test_a_stay_ending_on_the_hotels_today_has_ended",
    ),
    inactive(
        "H1-M9",
        "a room's stay ending today still blocks its deactivation",
        one("backend/app/repositories/room.py", HOLDS_AFTER, HOLDS_AFTER_OR_ON),
        "test_a_stay_ending_on_the_hotels_today_has_ended",
    ),
    inactive(
        "H1-M10",
        "a stay ending today on one of a type's rooms still blocks the type's deactivation",
        one("backend/app/repositories/room_type.py", HOLDS_AFTER, HOLDS_AFTER_OR_ON),
        "test_a_stay_ending_on_the_hotels_today_has_ended",
    ),
    inactive(
        "H1-M11",
        "a checked-in stay ahead no longer blocks a room's deactivation",
        one(
            "backend/app/repositories/room.py",
            HOLDING + HOLDS_AFTER,
            '                        BookingRoom.booking_status.in_(("confirmed",)),\n'
            + HOLDS_AFTER,
        ),
        "test_a_stay_ending_the_day_after_the_hotels_today_blocks",
    ),
    inactive(
        "H1-M12",
        "a sale no longer locks its rooms and their types",
        one(
            "backend/app/repositories/booking.py",
            "                .with_for_update(read=True)\n",
            "",
        ),
        "test_a_sale_holds_its_room_and_type_against_a_deactivation",
    ),
    inactive(
        "H1-M13",
        "deactivating a room no longer locks it against a concurrent sale",
        one(
            "backend/app/repositories/room.py", "            .with_for_update(key_share=True)\n", ""
        ),
        "test_a_deactivation_holds_its_room_against_a_sale",
    ),
    inactive(
        "H1-M14",
        "deactivating a room type no longer locks it against a concurrent sale",
        one(
            "backend/app/repositories/room_type.py",
            "            .with_for_update(key_share=True)\n",
            "",
        ),
        "test_a_deactivation_holds_its_room_against_a_sale",
    ),
    inactive(
        "H1-M15",
        "the delete refusal again advises deactivating a room that may still hold stays",
        one(
            "backend/app/services/room.py",
            '"reservations. Deactivate the room instead -- possible once it holds no "\n'
            '                    "confirmed or checked-in stay ending after today -- '
            'or remove those "\n'
            '                    "records first."\n',
            '"reservations. Deactivate the room instead, or remove those records "\n'
            '                    "first."\n',
        ),
        "test_the_delete_refusal_says_when_deactivation_is_possible",
    ),
    Mutation(
        "H1-M16",
        "H1",
        "the room page explains a refused withdrawal as a generic conflict",
        one(
            "frontend/src/pages/RoomDetailPage.tsx",
            "      'A room cannot be withdrawn from service while it holds a confirmed or "
            "checked-in stay ending after today. Cancel, move or check out those stays first. "
            "The room is unchanged.',",
            "      'The change conflicts with existing data. The room is unchanged.',",
        ),
        (f"{ROOMS_TESTS}::explains a refused withdrawal by the stays still to come",),
        runner=Runner.VITEST,
    ),
    Mutation(
        "H1-M17",
        "H1",
        "the property page explains a refused room-type withdrawal as an occupancy clash",
        one(
            "frontend/src/pages/PropertyPage.tsx",
            ", or making the type unbookable while one of its rooms holds a confirmed or "
            "checked-in stay ending after today. The room type is unchanged.',",
            ". The room type is unchanged.',",
        ),
        (
            f"{PROPERTY_TESTS}::"
            "explains a refused withdrawal by the stays still to come on its rooms",
        ),
        runner=Runner.VITEST,
    ),
    inactive(
        "H1-M18",
        "the extension route no longer declares the inactive-room 409 it can answer",
        one(
            "backend/app/api/v1/endpoints/bookings.py",
            "    responses={**NOT_FOUND_RESPONSE, **SALE_CONFLICT_RESPONSE},\n)\ndef extend_stay(",
            "    responses={**NOT_FOUND_RESPONSE, **CONFLICT_RESPONSE},\n)\ndef extend_stay(",
        ),
        "test_the_writes_that_sell_a_room_declare_the_inactive_room_conflict",
    ),
]


EARLY_DEPARTURE = "tests/integration/test_early_departure.py"
DEPARTURE_CONTRACT = "tests/backend/test_early_departure_contract.py"
BOOKING_MUTATIONS_TESTS = "src/features/bookings/mutations.test.tsx"
BOOKING_DETAIL_PAGE = "frontend/src/pages/BookingDetailPage.tsx"
DEPART_SUCCEEDS = (
    f"{EARLY_DEPARTURE}::test_an_early_departure_keeps_the_nights_stayed_and_removes_the_rest"
)
DEPARTURE_BOUND = f"{EARLY_DEPARTURE}::test_each_bound_of_the_departure_date"
DEPARTURE_EVENTS = f"{EARLY_DEPARTURE}::test_both_events_are_recorded"
DEPARTURE_MONEY = (
    f"{EARLY_DEPARTURE}::test_the_money_that_leaves_is_the_rates_of_the_nights_removed"
)
REMOVED_SUM = 'sum(removed, decimal.Decimal("0.00"))'
#: The departure's own repricing: inside its try block, so indented deeper than the preview's.
DEPARTURE_NEW_TOTAL = f"                new_total=previous_total - {REMOVED_SUM},\n"
#: The preview's, anchored on the line before it: at twelve spaces it is otherwise a suffix of
#: the departure's sixteen-space line.
PREVIEW_TOTALS = "\n            previous_total=previous_total,\n"
PREVIEW_NEW_TOTAL = f"{PREVIEW_TOTALS}            new_total=previous_total - {REMOVED_SUM},\n"
LATER_THAN_TODAY = "        if departure > today:\n"
#: The H2 correction: a departure on or before the newest accuracy-scorable night is refused.
NOT_AFTER_SCORED = "        if departure <= scored:\n"
NEWEST_SCORABLE = "        return today - dt.timedelta(days=DEPARTURE_LOOKBACK_DAYS)\n"


def departure(name: str, breaks: str, edits: tuple[Edit, ...], killer: str) -> Mutation:
    """An H2 mutation whose killer is a database-backed test."""
    return Mutation(name, "H2", breaks, edits, (killer,), needs_database=True)


STAGE_H2 = [
    departure(
        "H2-M1",
        "the nights after the departure are kept instead of deleted",
        one(
            BOOKING_SERVICE,
            "            removed = self._repository.delete_nights_from(booking.id, "
            "payload.departure_date)\n",
            "            removed = self._repository.nights_from(booking.id, "
            "payload.departure_date)\n",
        ),
        DEPART_SUCCEEDS,
    ),
    departure(
        "H2-M2",
        "the night of the departure date is kept: the boundary night is wrong",
        one(
            "backend/app/repositories/booking.py",
            "            BookingRoomNight.stay_date >= day,\n",
            "            BookingRoomNight.stay_date > day,\n",
        ),
        DEPART_SUCCEEDS,
    ),
    departure(
        "H2-M3",
        "the last night stayed is deleted too: [check_in, check_out) read as closed",
        one(
            "backend/app/repositories/booking.py",
            "            BookingRoomNight.stay_date >= day,\n",
            "            BookingRoomNight.stay_date >= day - dt.timedelta(days=1),\n",
        ),
        DEPART_SUCCEEDS,
    ),
    departure(
        "H2-M4",
        "the new check-out is the day after the departure",
        one(
            BOOKING_SERVICE,
            '                    "check_out_date": payload.departure_date,\n',
            '                    "check_out_date": payload.departure_date '
            "+ dt.timedelta(days=1),\n",
        ),
        DEPART_SUCCEEDS,
    ),
    departure(
        "H2-M5",
        "the booking stays checked in after its departure",
        one(
            BOOKING_SERVICE, '                    "status": BookingStatus.CHECKED_OUT.value,\n', ""
        ),
        DEPART_SUCCEEDS,
    ),
    departure(
        "H2-M6",
        "a departure on the planned check-out day is accepted",
        one(
            BOOKING_SERVICE,
            "        if departure >= booking.check_out_date:\n",
            "        if departure > booking.check_out_date:\n",
        ),
        DEPARTURE_BOUND,
    ),
    departure(
        "H2-M7",
        "a departure tomorrow is accepted",
        one(
            BOOKING_SERVICE,
            LATER_THAN_TODAY,
            "        if departure > today + dt.timedelta(days=1):\n",
        ),
        DEPARTURE_BOUND,
    ),
    departure(
        "H2-M8",
        "a departure today is refused",
        one(BOOKING_SERVICE, LATER_THAN_TODAY, "        if departure >= today:\n"),
        DEPARTURE_BOUND,
    ),
    departure(
        "H2-M9",
        "the lower bound is inclusive: a departure exactly 28 days ago is accepted",
        one(BOOKING_SERVICE, NOT_AFTER_SCORED, "        if departure < scored:\n"),
        DEPARTURE_BOUND,
    ),
    departure(
        "H2-M10",
        "the earliest departure offered is 28 days ago, not 27",
        one(
            BOOKING_SERVICE,
            "            max(booking.check_in_date + day, "
            "BookingService._newest_scorable(today) + day),\n",
            "            max(booking.check_in_date + day, "
            "BookingService._newest_scorable(today)),\n",
        ),
        f"{EARLY_DEPARTURE}::test_the_preview_range_is_bounded_by_the_stay_and_by_the_lookback",
    ),
    departure(
        "H2-M28",
        "the newest night out of reach is one day too old: an accuracy-scored night can go",
        one(BOOKING_SERVICE, NEWEST_SCORABLE, NEWEST_SCORABLE.replace("_DAYS)", "_DAYS + 1)")),
        f"{EARLY_DEPARTURE}::test_a_departure_cannot_mutate_an_accuracy_scored_night",
    ),
    departure(
        "H2-M29",
        "the newest night out of reach is one day too young: a departure 27 days ago is refused",
        one(BOOKING_SERVICE, NEWEST_SCORABLE, NEWEST_SCORABLE.replace("_DAYS)", "_DAYS - 1)")),
        DEPARTURE_BOUND,
    ),
    departure(
        "H2-M30",
        "there is no lower bound: a departure 29 days ago is accepted",
        one(BOOKING_SERVICE, NOT_AFTER_SCORED, "        if False:\n"),
        DEPARTURE_BOUND,
    ),
    departure(
        "H2-M11",
        "today is UTC's date, not the hotel's",
        one(
            BOOKING_SERVICE,
            "        return hotel_today(hotel, self._clock)\n",
            "        return self._clock().date()\n",
        ),
        f"{EARLY_DEPARTURE}::test_today_is_the_hotels_not_utcs",
    ),
    departure(
        "H2-M12",
        "a booking that is not checked in can depart early",
        one(
            BOOKING_SERVICE,
            "        if status == BookingStatus.CHECKED_IN.value:\n            return\n"
            '        raise ConflictError(f"Only a checked-in stay can depart early;',
            "        if True:\n            return\n"
            '        raise ConflictError(f"Only a checked-in stay can depart early;',
        ),
        f"{EARLY_DEPARTURE}::test_only_a_checked_in_stay_departs_early",
    ),
    departure(
        "H2-M13",
        "the departure decides before taking the booking's lock",
        one(
            BOOKING_SERVICE,
            "        booking = self._repository.lock_for_update(booking)\n"
            "        self._require_departable(booking.status)\n",
            "        self._require_departable(booking.status)\n",
        ),
        f"{EARLY_DEPARTURE}::"
        "test_a_departure_waits_for_a_concurrent_change_and_judges_what_it_committed",
    ),
    departure(
        "H2-M14",
        "the stay change is not audited",
        one(
            BOOKING_SERVICE,
            "            self._audit.record(\n                AuditAction.BOOKING_STAY_MODIFIED,\n"
            "                AuditResourceType.BOOKING,\n                str(booking.public_id),\n"
            "                hotel_id=hotel.id,\n                details={\n"
            '                    "changed_fields": ["check_out_date"],\n'
            '                    "previous_check_out_date": planned_check_out.isoformat(),\n',
            "            (lambda *_, **__: None)(\n"
            "                AuditAction.BOOKING_STAY_MODIFIED,\n"
            "                AuditResourceType.BOOKING,\n                str(booking.public_id),\n"
            "                hotel_id=hotel.id,\n                details={\n"
            '                    "changed_fields": ["check_out_date"],\n'
            '                    "previous_check_out_date": planned_check_out.isoformat(),\n',
        ),
        DEPARTURE_EVENTS,
    ),
    departure(
        "H2-M15",
        "the status change is not audited",
        one(
            BOOKING_SERVICE,
            "            self._audit.record(\n                AuditAction.BOOKING_STATUS_CHANGED,\n"
            "                AuditResourceType.BOOKING,\n                str(booking.public_id),\n"
            "                hotel_id=hotel.id,\n                details={\n"
            '                    "old_status": BookingStatus.CHECKED_IN.value,\n',
            "            (lambda *_, **__: None)(\n"
            "                AuditAction.BOOKING_STATUS_CHANGED,\n"
            "                AuditResourceType.BOOKING,\n                str(booking.public_id),\n"
            "                hotel_id=hotel.id,\n                details={\n"
            '                    "old_status": BookingStatus.CHECKED_IN.value,\n',
        ),
        DEPARTURE_EVENTS,
    ),
    departure(
        "H2-M16",
        "the refund is measured from the contracted total, not the nights",
        one(
            BOOKING_SERVICE,
            "                previous_total=previous_total,\n" + DEPARTURE_NEW_TOTAL,
            "                previous_total=booking.total_amount,\n"
            f"                new_total=booking.total_amount - {REMOVED_SUM},\n",
        ),
        DEPARTURE_MONEY,
    ),
    departure(
        "H2-M17",
        "the money that leaves is counted as nights times one rate, ignoring complimentary ones",
        one(
            BOOKING_SERVICE,
            DEPARTURE_NEW_TOTAL,
            "                new_total=previous_total - len(removed) * max(removed),\n",
        ),
        f"{EARLY_DEPARTURE}::test_a_complimentary_night_removed_takes_no_money_with_it",
    ),
    departure(
        "H2-M18",
        "the preview reports no change to the money",
        one(
            BOOKING_SERVICE,
            PREVIEW_NEW_TOTAL,
            f"{PREVIEW_TOTALS}            new_total=previous_total,\n",
        ),
        f"{EARLY_DEPARTURE}::test_the_preview_states_what_the_departure_then_does",
    ),
    departure(
        "H2-M19",
        "a plain check-out before the planned day is accepted again",
        one(
            BOOKING_SERVICE,
            "                self._require_departure_due(hotel, booking)\n",
            "                pass\n",
        ),
        f"{EARLY_DEPARTURE}::test_a_plain_check_out_before_the_planned_day_is_refused",
    ),
    departure(
        "H2-M20",
        "a plain check-out on the planned day is refused",
        one(
            BOOKING_SERVICE,
            "        if today < booking.check_out_date:\n",
            "        if today <= booking.check_out_date:\n",
        ),
        f"{EARLY_DEPARTURE}::test_a_plain_check_out_on_or_after_the_planned_day_is_unchanged",
    ),
    departure(
        "H2-M21",
        "the audit records one night fewer than were removed",
        one(
            BOOKING_SERVICE,
            '                    "nights_removed": len(removed),\n',
            '                    "nights_removed": len(removed) - 1,\n',
        ),
        DEPARTURE_EVENTS,
    ),
    Mutation(
        "H2-M22",
        "H2",
        "the departure's look-back is no longer the accuracy protocol's settlement lag",
        one(
            BOOKING_SERVICE,
            "DEPARTURE_LOOKBACK_DAYS = SETTLEMENT_LAG_DAYS\n",
            "DEPARTURE_LOOKBACK_DAYS = SETTLEMENT_LAG_DAYS + 1\n",
        ),
        (f"{DEPARTURE_CONTRACT}::test_the_lookback_is_the_accuracy_protocols_settlement_lag",),
    ),
    Mutation(
        "H2-M23",
        "H2",
        "the accuracy protocol no longer states the departure's 28-day boundary",
        one(
            "backend/app/ml/accuracy_protocol.py",
            "hotel's today minus 27 days. This protects nights that may already have entered "
            "accuracy scoring --",
            "hotel's today minus 28 days. This affects nights --",
        ),
        (f"{DEPARTURE_CONTRACT}::test_the_protocol_states_the_departure_boundary_and_why",),
    ),
    Mutation(
        "H2-M24",
        "H2",
        "checking an in-house guest out early goes straight to the plain check-out again",
        one(
            BOOKING_DETAIL_PAGE,
            "        booking.check_out_date > today\n",
            "        booking.check_out_date > '9999-12-31'\n",
        ),
        (
            f"{BOOKING_MUTATIONS_TESTS}::"
            "opens the early-departure form instead of checking the guest out",
        ),
        runner=Runner.VITEST,
    ),
    Mutation(
        "H2-M25",
        "H2",
        "the departure form opens on the browser's UTC date, not the hotel's",
        one(
            BOOKING_DETAIL_PAGE,
            "  const today = todayInZone(timeZone)\n",
            "  const today = todayInZone('UTC')\n",
        ),
        (
            f"{BOOKING_MUTATIONS_TESTS}::opens on the hotel{TYPOGRAPHIC_APOSTROPHE}s own today, "
            f"not the browser{TYPOGRAPHIC_APOSTROPHE}s UTC date",
        ),
        runner=Runner.VITEST,
    ),
    Mutation(
        "H2-M26",
        "H2",
        "the departure form posts to the extension route",
        one(
            "frontend/src/services/bookings/bookingService.ts",
            "    return api.post<StayModificationResponse>(\n"
            "      `/hotels/${hotelPublicId}/bookings/${bookingPublicId}/stay/departure`,\n",
            "    return api.post<StayModificationResponse>(\n"
            "      `/hotels/${hotelPublicId}/bookings/${bookingPublicId}/stay/extension`,\n",
        ),
        (
            f"{BOOKING_MUTATIONS_TESTS}::"
            "records the departure with one field, by POST, on the departure route",
        ),
        runner=Runner.VITEST,
    ),
    Mutation(
        "H2-M27",
        "H2",
        "the departure form ignores the server's range of dates",
        one(
            "frontend/src/features/bookings/EarlyDepartureForm.tsx",
            "min: preview.earliest_departure_date,",
            "min: undefined,",
        ),
        (
            f"{BOOKING_MUTATIONS_TESTS}::"
            f"offers only the server{TYPOGRAPHIC_APOSTROPHE}s range of dates",
        ),
        runner=Runner.VITEST,
    ),
]


SET_NULL_MIGRATION = "database/migrations/versions/20261006_0018_composite_set_null_columns.py"
INITIAL_SCHEMA = "database/migrations/versions/20260828_0001_initial_schema.py"
SET_NULL_TESTS = "tests/integration/test_set_null_detach.py"
REVIEW_DETACHED = f"{SET_NULL_TESTS}::test_deleting_a_booking_detaches_its_review"
REVENUE_DETACHED = f"{SET_NULL_TESTS}::test_deleting_a_booking_detaches_its_revenue"
GUEST_DETACHED = f"{SET_NULL_TESTS}::test_the_guest_key_detaches_on_its_own_in_the_database"
MODELS_MATCH = f"{SET_NULL_TESTS}::test_the_models_match_the_migrated_schema"
PAYMENT_STILL_BLOCKS = f"{SET_NULL_TESTS}::test_a_booking_with_a_payment_still_cannot_be_deleted"
BOOKING_STILL_BLOCKS = f"{SET_NULL_TESTS}::test_a_guest_with_a_booking_still_cannot_be_deleted"

#: Each 0018 key's own entry, ending at its ON DELETE action.
SET_NULL_KEYS = {
    "revenue": (
        '        "fk_revenue_booking_id_hotel_id_bookings",\n'
        '        "booking_id",\n        "bookings",\n        "SET NULL (booking_id)",\n'
    ),
    "review-booking": (
        '        "fk_reviews_booking_id_hotel_id_bookings",\n'
        '        "booking_id",\n        "bookings",\n        "SET NULL (booking_id)",\n'
    ),
    "review-guest": (
        '        "fk_reviews_guest_id_hotel_id_guests",\n'
        '        "guest_id",\n        "guests",\n        "SET NULL (guest_id)",\n'
    ),
}
KILLER_OF_KEY = {
    "revenue": REVENUE_DETACHED,
    "review-booking": REVIEW_DETACHED,
    "review-guest": GUEST_DETACHED,
}


def set_null_key(number: int, key: str, action: str, breaks: str) -> Mutation:
    """One 0018 key recreated with *action* instead of its column-list SET NULL."""
    anchor = SET_NULL_KEYS[key]
    column = "guest_id" if key == "review-guest" else "booking_id"
    return Mutation(
        f"H3-M{number}",
        "H3",
        breaks,
        one(SET_NULL_MIGRATION, anchor, anchor.replace(f'"SET NULL ({column})"', f'"{action}"')),
        (KILLER_OF_KEY[key],),
        needs_database=True,
    )


STAGE_H3 = [
    set_null_key(1, "revenue", "SET NULL", "the revenue key nulls hotel_id too, so it cannot fire"),
    set_null_key(2, "review-booking", "SET NULL", "the review-booking key nulls hotel_id too"),
    set_null_key(3, "review-guest", "SET NULL", "the review-guest key nulls hotel_id too"),
    set_null_key(4, "revenue", "RESTRICT", "revenue blocks its booking's deletion"),
    set_null_key(5, "review-booking", "RESTRICT", "a review blocks its booking's deletion"),
    set_null_key(6, "review-guest", "RESTRICT", "a review blocks its author's deletion"),
    set_null_key(7, "revenue", "CASCADE", "deleting a booking deletes its revenue lines"),
    set_null_key(8, "review-booking", "CASCADE", "deleting a booking deletes its review"),
    set_null_key(9, "review-guest", "CASCADE", "deleting a guest deletes their reviews"),
    Mutation(
        "H3-M10",
        "H3",
        "the downgrade does not restore 0001's keys",
        one(SET_NULL_MIGRATION, 'BEFORE = "SET NULL"\n', 'BEFORE = "RESTRICT"\n'),
        (
            f"{SET_NULL_TESTS}::"
            "test_the_downgrade_restores_0001s_keys_and_the_upgrade_reapplies_0018",
        ),
        needs_database=True,
    ),
    Mutation(
        "H3-M11",
        "H3",
        "the review model declares a bare SET NULL the migration no longer has",
        one(
            "backend/app/models/review.py",
            '            ondelete="SET NULL (booking_id)",\n',
            '            ondelete="SET NULL",\n',
        ),
        (MODELS_MATCH,),
        needs_database=True,
    ),
    Mutation(
        "H3-M12",
        "H3",
        "the review model lets hotel_id be null",
        one(
            "backend/app/models/review.py",
            '        BigInteger, ForeignKey("hotels.id", ondelete="CASCADE"), nullable=False\n',
            '        BigInteger, ForeignKey("hotels.id", ondelete="CASCADE"), nullable=True\n',
        ),
        (MODELS_MATCH,),
        needs_database=True,
    ),
    Mutation(
        "H3-M13",
        "H3",
        "the revenue model makes booking_id mandatory",
        one(
            "backend/app/models/finance.py",
            "    booking_id: Mapped[int | None] = mapped_column(BigInteger)\n",
            "    booking_id: Mapped[int | None] = mapped_column(BigInteger, nullable=False)\n",
        ),
        (MODELS_MATCH,),
        needs_database=True,
    ),
    Mutation(
        "H3-M14",
        "H3",
        "payments no longer protect a booking from deletion",
        one(
            INITIAL_SCHEMA,
            "REFERENCES bookings (id, hotel_id) ON DELETE RESTRICT,",
            "REFERENCES bookings (id, hotel_id) ON DELETE CASCADE,",
        ),
        (PAYMENT_STILL_BLOCKS,),
        needs_database=True,
    ),
    Mutation(
        "H3-M15",
        "H3",
        "a guest's bookings no longer protect the guest from deletion",
        one(
            INITIAL_SCHEMA,
            "REFERENCES guests (id, hotel_id) ON DELETE RESTRICT,",
            "REFERENCES guests (id, hotel_id) ON DELETE CASCADE,",
        ),
        (BOOKING_STILL_BLOCKS,),
        needs_database=True,
    ),
    Mutation(
        "H3-M16",
        "H3",
        "a detached review is rendered as though its booking still existed",
        one(
            "backend/app/services/review.py",
            "                bookings.get(review.booking_id) "
            "if review.booking_id is not None else None,\n",
            "                bookings[review.booking_id],\n",
        ),
        (REVIEW_DETACHED,),
        needs_database=True,
    ),
    Mutation(
        "H3-M17",
        "H3",
        "a detached revenue line is rendered as though its booking still existed",
        one(
            "backend/app/services/finance.py",
            "                bookings.get(row.booking_id) "
            "if row.booking_id is not None else None,\n",
            "                bookings[row.booking_id],\n",
        ),
        (REVENUE_DETACHED,),
        needs_database=True,
    ),
    Mutation(
        "H3-M18",
        "H3",
        "the hotel's review list joins to bookings and drops every detached review",
        one(
            "backend/app/repositories/review.py",
            "        statement = self._filtered(select(Review), hotel_id, source, is_published)\n",
            "        statement = self._filtered(\n"
            "            select(Review).join(Booking, Booking.id == Review.booking_id),\n"
            "            hotel_id,\n            source,\n            is_published,\n        )\n",
        ),
        (REVIEW_DETACHED,),
        needs_database=True,
    ),
    Mutation(
        "H3-M19",
        "H3",
        "the booking refusal still blames revenue and reviews",
        one(
            "backend/app/services/booking.py",
            '                    "This booking cannot be deleted because '
            'payments have been recorded "\n'
            '                    "against it. Cancel the booking instead."\n',
            '                    "This booking cannot be deleted because '
            'payments, revenue or reviews "\n'
            '                    "still reference it. Cancel the booking instead."\n',
        ),
        (PAYMENT_STILL_BLOCKS,),
        needs_database=True,
    ),
    Mutation(
        "H3-M20",
        "H3",
        "the guest refusal still blames reviews",
        one(
            "backend/app/services/guest.py",
            '                    "This guest cannot be deleted because '
            'existing reservations still "\n'
            '                    "reference them. Remove those reservations first."\n',
            '                    "This guest cannot be deleted because '
            'existing reservations or reviews "\n'
            '                    "still reference them. Remove those reservations first."\n',
        ),
        (BOOKING_STILL_BLOCKS,),
        needs_database=True,
    ),
]


TENANT_MIGRATION = "database/migrations/versions/20261007_0019_review_external_id_scope.py"
TENANT_TESTS = "tests/integration/test_review_external_id_scope.py"
REVIEWS_API_TESTS = "tests/integration/test_reviews_api.py"
REVIEW_LAYERING = "tests/backend/test_review_layering.py"
REVIEW_SERVICE = "backend/app/services/review.py"
REVIEW_MODEL = "backend/app/models/review.py"
REVIEWS_PAGE = "frontend/src/pages/ReviewsPage.tsx"
REVIEW_UI_TESTS = "src/features/reviews/reviews.test.tsx"
REVIEW_ARCH_TESTS = "src/features/reviews/architecture.node.test.ts"

INDEX_IS_APPROVED = f"{TENANT_TESTS}::test_the_index_is_exactly_the_approved_definition"
SAME_HOTEL_REFUSED = (
    f"{TENANT_TESTS}::test_the_same_hotel_cannot_record_a_reference_twice_for_one_source"
)
OTHER_HOTEL_ACCEPTED = f"{TENANT_TESTS}::test_different_hotels_may_record_the_same_reference"
NULLS_UNRESTRICTED = f"{TENANT_TESTS}::test_reviews_without_a_reference_are_unrestricted"
OWN_CODE = f"{TENANT_TESTS}::test_the_api_refuses_a_hotels_own_duplicate_with_its_own_code"
TENANTS_ISOLATED = f"{TENANT_TESTS}::test_tenants_are_isolated_both_ways"
DOWNGRADE_RESTORES = (
    f"{TENANT_TESTS}::test_the_downgrade_restores_0001s_index_and_the_upgrade_reapplies_0019"
)
UPGRADE_PRECHECK = f"{TENANT_TESTS}::test_the_upgrade_refuses_rows_the_new_key_cannot_hold"
API_DUPLICATE_409 = f"{REVIEWS_API_TESTS}::test_a_duplicate_external_identifier_returns_409"
API_OTHER_HOTEL = f"{REVIEWS_API_TESTS}::test_another_hotel_may_record_the_same_external_identifier"
API_NULLS_PARTIAL = (
    f"{REVIEWS_API_TESTS}::test_the_external_index_is_partial_so_direct_reviews_are_unconstrained"
)
MODEL_KEYS = f"{REVIEW_LAYERING}::test_the_two_partial_unique_keys_still_exist"
CASES_COLLECTED = f"{REVIEW_LAYERING}::test_the_tenant_scope_cases_are_still_collected"
UI_DUPLICATE_COPY = (
    f"{REVIEW_UI_TESTS}::explains DUPLICATE_EXTERNAL_REVIEW as a platform reference already "
    "recorded here"
)
UI_STAY_RULE = f"{REVIEW_UI_TESTS}::keeps the stay rule for CONFLICT, whatever the message says"
UI_NEVER_READS = f"{REVIEW_ARCH_TESTS}::never reads or matches a server message"
UI_BY_CODE = f"{REVIEW_ARCH_TESTS}::chooses the duplicate-reference copy by the structured code"

#: 0019's one statement that builds an index, in both directions.
CREATE_STATEMENT = (
    '    op.execute(f"CREATE UNIQUE INDEX {create} ON reviews ({columns}) WHERE {PREDICATE}")\n'
)
UPGRADE_SWAP = "    _replace(drop=GLOBAL_INDEX, create=TENANT_INDEX, columns=TENANT_COLUMNS)\n"
MODEL_KEY = (
    '            "uq_reviews_hotel_source_external_review_id",\n'
    '            "hotel_id",\n'
    '            "source",\n'
)
CHOSEN_COPY = "    ? { ...CREATION_FAILURE_COPY, conflict: DUPLICATE_REFERENCE_COPY }\n"


def tenant_db(number: int, breaks: str, path: str, old: str, new: str, *killers: str) -> Mutation:
    """An H4 mutation whose killers run against PostgreSQL."""
    return Mutation(
        f"H4-M{number}", "H4", breaks, one(path, old, new), killers, needs_database=True
    )


def tenant_static(number: int, breaks: str, path: str, old: str, new: str, killer: str) -> Mutation:
    """An H4 mutation caught without a database: a pytest source pin."""
    return Mutation(f"H4-M{number}", "H4", breaks, one(path, old, new), (killer,))


def tenant_ui(number: int, breaks: str, path: str, old: str, new: str, *killers: str) -> Mutation:
    """An H4 mutation of the Reviews page or client, caught by Vitest."""
    return Mutation(
        f"H4-M{number}", "H4", breaks, one(path, old, new), killers, runner=Runner.VITEST
    )


STAGE_H4 = [
    tenant_db(
        1,
        "the new index leaves hotel_id out, so it is global again under the new name",
        TENANT_MIGRATION,
        'TENANT_COLUMNS = "hotel_id, source, external_review_id"\n',
        'TENANT_COLUMNS = "source, external_review_id"\n',
        OTHER_HOTEL_ACCEPTED,
        INDEX_IS_APPROVED,
    ),
    tenant_db(
        2,
        "the upgrade leaves 0001's global key in place",
        TENANT_MIGRATION,
        UPGRADE_SWAP,
        "    pass\n",
        INDEX_IS_APPROVED,
        API_OTHER_HOTEL,
        TENANTS_ISOLATED,
    ),
    tenant_db(
        3,
        "the index loses its external_review_id IS NOT NULL predicate",
        TENANT_MIGRATION,
        CREATE_STATEMENT,
        '    op.execute(f"CREATE UNIQUE INDEX {create} ON reviews ({columns})")\n',
        INDEX_IS_APPROVED,
        DOWNGRADE_RESTORES,
    ),
    tenant_db(
        4,
        "reviews without a reference collide with each other",
        TENANT_MIGRATION,
        CREATE_STATEMENT,
        '    op.execute(f"CREATE UNIQUE INDEX {create} ON reviews ({columns}) '
        'NULLS NOT DISTINCT")\n',
        NULLS_UNRESTRICTED,
        API_NULLS_PARTIAL,
    ),
    tenant_db(
        5,
        "the duplicate-reference refusal carries a different code",
        REVIEW_SERVICE,
        'DUPLICATE_EXTERNAL_REVIEW = "DUPLICATE_EXTERNAL_REVIEW"\n',
        'DUPLICATE_EXTERNAL_REVIEW = "DUPLICATE_REVIEW_REFERENCE"\n',
        OWN_CODE,
    ),
    tenant_db(
        6,
        "the duplicate-reference refusal falls back to the generic CONFLICT",
        REVIEW_SERVICE,
        "                    DUPLICATE_EXTERNAL_REVIEW_MESSAGE, code=DUPLICATE_EXTERNAL_REVIEW\n",
        "                    DUPLICATE_EXTERNAL_REVIEW_MESSAGE\n",
        OWN_CODE,
        API_DUPLICATE_409,
        TENANTS_ISOLATED,
    ),
    tenant_db(
        7,
        "the service still listens for 0001's index name, so the refusal is never recognised",
        REVIEW_SERVICE,
        'EXTERNAL_ID_CONSTRAINT = "uq_reviews_hotel_source_external_review_id"\n',
        'EXTERNAL_ID_CONSTRAINT = "uq_reviews_source_external_review_id"\n',
        OWN_CODE,
        MODEL_KEYS,
    ),
    tenant_db(
        8,
        "the model still declares 0001's global key",
        REVIEW_MODEL,
        MODEL_KEY,
        '            "uq_reviews_source_external_review_id",\n            "source",\n',
        MODEL_KEYS,
        MODELS_MATCH,
    ),
    tenant_db(
        9,
        "the model's key leaves hotel_id out",
        REVIEW_MODEL,
        MODEL_KEY,
        '            "uq_reviews_hotel_source_external_review_id",\n            "source",\n',
        MODEL_KEYS,
        MODELS_MATCH,
    ),
    tenant_db(
        10,
        "the index is no longer unique, so a hotel may record a reference twice",
        TENANT_MIGRATION,
        CREATE_STATEMENT,
        '    op.execute(f"CREATE INDEX {create} ON reviews ({columns}) WHERE {PREDICATE}")\n',
        SAME_HOTEL_REFUSED,
        OWN_CODE,
        API_DUPLICATE_409,
    ),
    tenant_db(
        11,
        "the upgrade adds the per-hotel key but keeps the global one: other hotels still collide",
        TENANT_MIGRATION,
        UPGRADE_SWAP,
        '    op.execute(f"CREATE UNIQUE INDEX {TENANT_INDEX} ON reviews ({TENANT_COLUMNS}) '
        'WHERE {PREDICATE}")\n',
        OTHER_HOTEL_ACCEPTED,
        API_OTHER_HOTEL,
        INDEX_IS_APPROVED,
    ),
    tenant_db(
        12,
        "the downgrade rebuilds the per-hotel key instead of 0001's global one",
        TENANT_MIGRATION,
        "    _replace(drop=TENANT_INDEX, create=GLOBAL_INDEX, columns=GLOBAL_COLUMNS)\n",
        "    _replace(drop=TENANT_INDEX, create=GLOBAL_INDEX, columns=TENANT_COLUMNS)\n",
        DOWNGRADE_RESTORES,
    ),
    tenant_db(
        13,
        "the upgrade no longer counts the rows its key would refuse",
        TENANT_MIGRATION,
        "    found = _duplicates(TENANT_COLUMNS)\n",
        "    found = 0\n",
        UPGRADE_PRECHECK,
    ),
    tenant_ui(
        14,
        "the page words DUPLICATE_EXTERNAL_REVIEW as the stay already having a review",
        REVIEWS_PAGE,
        CHOSEN_COPY,
        "    ? CREATION_FAILURE_COPY\n",
        UI_DUPLICATE_COPY,
    ),
    tenant_ui(
        15,
        "the client's copy of the code no longer matches the server's",
        "frontend/src/services/reviews/reviewService.ts",
        "export const DUPLICATE_EXTERNAL_REVIEW = 'DUPLICATE_EXTERNAL_REVIEW'\n",
        "export const DUPLICATE_EXTERNAL_REVIEW = 'DUPLICATE_REVIEW_REFERENCE'\n",
        UI_DUPLICATE_COPY,
        UI_BY_CODE,
    ),
    tenant_ui(
        16,
        "the page tells the two 409s apart by matching the server's words",
        REVIEWS_PAGE,
        "  return error.code === DUPLICATE_EXTERNAL_REVIEW\n",
        "  return error.message.includes('platform reference')\n",
        UI_NEVER_READS,
        UI_STAY_RULE,
    ),
    tenant_ui(
        17,
        "the page shows the server's own message for a duplicate reference",
        REVIEWS_PAGE,
        CHOSEN_COPY,
        "    ? { ...CREATION_FAILURE_COPY, conflict: { ...DUPLICATE_REFERENCE_COPY, "
        "detail: error.message } }\n",
        UI_DUPLICATE_COPY,
        UI_NEVER_READS,
    ),
    tenant_static(
        18,
        "the tenant-isolation case is no longer collected",
        TENANT_TESTS,
        "def test_tenants_are_isolated_both_ways(",
        "def _tenants_are_isolated_both_ways(",
        CASES_COLLECTED,
    ),
    tenant_static(
        19,
        "the API's cross-hotel acceptance case is no longer collected",
        REVIEWS_API_TESTS,
        "def test_another_hotel_may_record_the_same_external_identifier(",
        "def _another_hotel_may_record_the_same_external_identifier(",
        CASES_COLLECTED,
    ),
    tenant_static(
        20,
        "the NULL-reference case is no longer collected",
        TENANT_TESTS,
        "def test_reviews_without_a_reference_are_unrestricted(",
        "def _reviews_without_a_reference_are_unrestricted(",
        CASES_COLLECTED,
    ),
]


OCC_TESTS = "tests/integration/test_optimistic_concurrency.py"
PRECONDITION_TESTS = "tests/backend/test_preconditions.py"
STALE_REFUSED = f"{OCC_TESTS}::test_a_stale_version_is_refused_and_nothing_is_written"
WAITS_FOR_WRITER = (
    f"{OCC_TESTS}::test_a_writer_that_commits_while_the_update_waits_is_seen_not_overwritten"
)
MALFORMED_IS_422 = (
    f"{PRECONDITION_TESTS}::test_a_malformed_precondition_is_a_validation_error_at_the_header"
)
OTHER_INSTANT_STALE = f"{PRECONDITION_TESTS}::test_any_other_instant_is_stale_earlier_or_later"
GUEST_UI = "src/features/guests/guests.test.tsx"
PROPERTY_UI = "src/features/property/property.test.tsx"
ROOMS_UI = "src/features/rooms/rooms.test.tsx"
PLATFORM_UI = "src/features/platform/platform.test.tsx"
PRECONDITIONS_UI = "src/services/api/preconditions.test.ts"
GUEST_SENDS_DIFF = (
    f"{GUEST_UI}::sends the changed field alone, with the loaded updated_at as If-Match"
)
GUEST_STALE_KEPT = (
    f"{GUEST_UI}::keeps the draft on a stale save, retries nothing, and reloads only when asked"
)


def stale_check(number: int, service: str, name: str, resource: str) -> Mutation:
    """One service's precondition taken out: lock and comparison both."""
    return Mutation(
        f"H6-M{number}",
        "H6",
        f"a stale If-Match on the {resource} PATCH is ignored and the write lands",
        one(
            f"backend/app/services/{service}.py",
            f"            {name} = self._repository.lock_for_update({name})\n"
            f"            require_unchanged({name}.updated_at, expected_updated_at)\n",
            "            pass\n",
        ),
        (f"{STALE_REFUSED}[{resource}]",),
        needs_database=True,
    )


def precondition_static(
    number: int, breaks: str, path: str, old: str, new: str, *killers: str
) -> Mutation:
    """An H6 mutation caught by a test that needs no database."""
    return Mutation(f"H6-M{number}", "H6", breaks, one(path, old, new), killers)


def precondition_ui(
    number: int, breaks: str, path: str, old: str, new: str, *killers: str
) -> Mutation:
    """An H6 mutation of the client, caught by Vitest."""
    return Mutation(
        f"H6-M{number}", "H6", breaks, one(path, old, new), killers, runner=Runner.VITEST
    )


STAGE_H6 = [
    stale_check(1, "guest", "guest", "guest"),
    stale_check(2, "hotel", "hotel", "hotel"),
    stale_check(3, "room", "room", "room"),
    stale_check(4, "room_type", "room_type", "room-type"),
    stale_check(5, "booking", "booking", "booking"),
    Mutation(
        "H6-M6",
        "H6",
        "the version is compared before the row lock, so a writer the update waits for is missed",
        one(
            "backend/app/services/guest.py",
            "            guest = self._repository.lock_for_update(guest)\n"
            "            require_unchanged(guest.updated_at, expected_updated_at)\n",
            "            require_unchanged(guest.updated_at, expected_updated_at)\n"
            "            guest = self._repository.lock_for_update(guest)\n",
        ),
        (WAITS_FOR_WRITER,),
        needs_database=True,
    ),
    Mutation(
        "H6-M7",
        "H6",
        "the locked re-read keeps the identity map's stale updated_at",
        one(
            "backend/app/repositories/guest.py",
            "            .execution_options(populate_existing=True)\n",
            "",
        ),
        (WAITS_FOR_WRITER,),
        needs_database=True,
    ),
    Mutation(
        "H6-M8",
        "H6",
        "the route no longer passes the precondition to the service",
        one(
            "backend/app/api/v1/endpoints/guests.py",
            "    return service.update(hotel_public_id, guest_public_id, payload, "
            "expected_updated_at=if_match)\n",
            "    return service.update(hotel_public_id, guest_public_id, payload)\n",
        ),
        (f"{STALE_REFUSED}[guest]",),
        needs_database=True,
    ),
    Mutation(
        "H6-M9",
        "H6",
        "an empty update returns before the precondition is checked",
        one(
            "backend/app/services/guest.py",
            "        if expected_updated_at is not None:\n"
            "            # Issue H6. Lock, then compare",
            "        if not payload.model_dump(exclude_unset=True):\n"
            "            return self._to_response(guest, hotel)\n"
            "        if expected_updated_at is not None:\n"
            "            # Issue H6. Lock, then compare",
        ),
        (f"{OCC_TESTS}::test_an_empty_update_is_still_checked[guest]",),
        needs_database=True,
    ),
    precondition_static(
        10,
        "versions are compared by order, so a newer stamp than the client's passes",
        "backend/app/services/concurrency.py",
        "    if expected is not None and current != expected:\n",
        "    if expected is not None and current < expected:\n",
        OTHER_INSTANT_STALE,
    ),
    precondition_static(
        11,
        "a weak entity-tag is no longer refused for what it is",
        "backend/app/api/preconditions.py",
        '    if text.startswith("W/"):\n',
        '    if text.startswith("W/") and False:\n',
        MALFORMED_IS_422,
    ),
    precondition_static(
        12,
        "a timestamp finer than a microsecond is silently truncated instead of refused",
        "backend/app/api/preconditions.py",
        "    if SUB_MICROSECOND.search(content):\n",
        "    if False:\n",
        MALFORMED_IS_422,
    ),
    precondition_static(
        13,
        "a timestamp without a zone is accepted",
        "backend/app/api/preconditions.py",
        "    if stamp.tzinfo is None:\n",
        "    if False:\n",
        MALFORMED_IS_422,
    ),
    precondition_static(
        14,
        "a stale update is answered 409, not 412",
        "backend/app/core/errors.py",
        "    status_code = status.HTTP_412_PRECONDITION_FAILED\n",
        "    status_code = status.HTTP_409_CONFLICT\n",
        OTHER_INSTANT_STALE,
    ),
    precondition_static(
        15,
        "a stale update carries the generic CONFLICT code",
        "backend/app/core/errors.py",
        '    code = "STALE_UPDATE"\n',
        '    code = "CONFLICT"\n',
        OTHER_INSTANT_STALE,
    ),
    precondition_static(
        16,
        "the guest PATCH no longer documents its 412",
        "backend/app/api/v1/endpoints/guests.py",
        "    responses={**NOT_FOUND_RESPONSE, **CONFLICT_RESPONSE, **STALE_UPDATE_RESPONSE},\n",
        "    responses={**NOT_FOUND_RESPONSE, **CONFLICT_RESPONSE},\n",
        f"{PRECONDITION_TESTS}::test_exactly_the_five_conditional_operations_declare_412",
    ),
    precondition_static(
        17,
        "the mutation harness counts an ERROR as a kill again",
        "tests/mutation/harness.py",
        'SUMMARY_WORDS = (("PASSED", "passed"), ("FAILED", "failed"), ("ERROR", "error"))\n',
        'SUMMARY_WORDS = (("PASSED", "passed"), ("FAILED", "failed"), ("ERROR", "failed"))\n',
        "tests/mutation/test_harness.py::test_an_error_is_reported_as_an_error_not_a_failure",
    ),
    precondition_ui(
        18,
        "the guest form sends every field again",
        "frontend/src/features/guests/GuestForm.tsx",
        "        changedFields(updateFrom(draftFrom(guest)), updateFrom(draft)),\n",
        "        updateFrom(draft),\n",
        GUEST_SENDS_DIFF,
    ),
    precondition_ui(
        19,
        "the property form sends every field again, time zone and currency included",
        "frontend/src/features/property/HotelForm.tsx",
        "    onUpdate(changedFields(updateFrom(draftFrom(hotel)),"
        " updateFrom(draft)), hotel.updated_at)\n",
        "    onUpdate(updateFrom(draft), hotel.updated_at)\n",
        f"{PROPERTY_UI}::sends one changed hotel field with the loaded updated_at as If-Match",
    ),
    precondition_ui(
        20,
        "the room form sends every field again, the loaded status included",
        "frontend/src/features/rooms/RoomForm.tsx",
        "      onUpdate?.(changedFields(updateFrom(draftFrom(room)),"
        " updateFrom(draft)), room.updated_at)\n",
        "      onUpdate?.(updateFrom(draft), room.updated_at)\n",
        f"{ROOMS_UI}::sends a notes edit alone -- never the stale status -- with If-Match",
    ),
    precondition_ui(
        21,
        "the room-type form sends every field again",
        "frontend/src/features/property/RoomTypeForm.tsx",
        "        changedFields(updateFrom(draftFrom(roomType,"
        " defaultCurrency)), updateFrom(draft)),\n",
        "        updateFrom(draft),\n",
        f"{PROPERTY_UI}::sends one changed room-type field with the loaded updated_at as If-Match",
    ),
    precondition_ui(
        22,
        "the catalogue form sends every field again",
        "frontend/src/features/platform/CatalogueForm.tsx",
        "      onSubmit(changedFields(payloadFrom(kind, valuesOf(entry)), shared))\n",
        "      onSubmit(shared)\n",
        f"{PLATFORM_UI}::sends a retired revenue category as is_active alone",
    ),
    precondition_ui(
        23,
        "the guest client no longer sends If-Match",
        "frontend/src/services/guests/guestService.ts",
        "      ...(version === undefined ? {} : { headers: ifMatch(version) }),\n",
        "",
        GUEST_SENDS_DIFF,
    ),
    precondition_ui(
        24,
        "a stale update is described as a server fault, with a retry and no reload",
        "frontend/src/services/api/failures.ts",
        "  if (error.status === 412 && error.code === STALE_UPDATE) {\n",
        "  if (error.status === 412 && error.code === 'NEVER') {\n",
        f"{PRECONDITIONS_UI}::offers a reload, not a retry",
        GUEST_STALE_KEPT,
    ),
    precondition_ui(
        25,
        "a stale update is recognised by its status alone, not its code",
        "frontend/src/services/api/failures.ts",
        "  if (error.status === 412 && error.code === STALE_UPDATE) {\n",
        "  if (error.status === 412) {\n",
        f"{PRECONDITIONS_UI}::goes by the code alone: a 412 with another code is not stale",
        f"{GUEST_UI}::recognises a stale save by its code, not its words",
    ),
    precondition_ui(
        26,
        "a reloaded guest does not replace the open form's draft",
        "frontend/src/pages/GuestDetailPage.tsx",
        "            key={guest.updated_at}\n",
        "",
        GUEST_STALE_KEPT,
    ),
    precondition_ui(
        27,
        "a reloaded property does not replace the open form's draft",
        "frontend/src/pages/PropertyPage.tsx",
        "              key={admin.hotel.updated_at}\n",
        "",
        f"{PROPERTY_UI}::keeps the hotel draft on a stale save and reloads only when asked",
    ),
    precondition_ui(
        28,
        "a reloaded room type does not replace the open form's draft",
        "frontend/src/pages/PropertyPage.tsx",
        "              key={editingType.updated_at}\n",
        "",
        f"{PROPERTY_UI}::keeps the room-type draft on a"
        " stale save; a reload takes the reloaded row",
    ),
    precondition_ui(
        29,
        "a reloaded room does not replace the open form's draft",
        "frontend/src/pages/RoomDetailPage.tsx",
        "            key={room.updated_at}\n",
        "",
        f"{ROOMS_UI}::keeps the draft on a stale save and reloads only when asked",
    ),
    precondition_ui(
        30,
        "the diff keeps every field, changed or not",
        "frontend/src/services/api/preconditions.ts",
        "    if (after[key] !== before[key]) {\n",
        "    if (after[key] !== before[key] || true) {\n",
        f"{PRECONDITIONS_UI}::is empty when nothing changed",
    ),
]


GUEST_EMAIL_MIGRATION = "database/migrations/versions/20261009_0020_guest_email_rules.py"
GUEST_EMAIL_UNIT = "tests/backend/test_guest_email.py"
GUEST_EMAIL_RULES = "tests/integration/test_guest_email_rules.py"
GUEST_FORM_UI = "src/features/guests/guests.test.tsx"
MALFORMED_REFUSED = f"{GUEST_EMAIL_UNIT}::test_a_malformed_address_is_refused_at_the_field"
LOWER_CASED = f"{GUEST_EMAIL_UNIT}::test_an_address_is_trimmed_and_lower_cased_on_create_and_update"
ONE_PATTERN = (
    f"{GUEST_EMAIL_UNIT}::test_the_api_the_database_the_frontend_and_users_share_one_pattern"
)
EMAIL_KEY_MODEL = (
    "tests/backend/test_guest_layering.py::"
    "test_the_email_uniqueness_is_partial_per_hotel_and_case_insensitive"
)
CASE_DUPLICATES_REFUSED = (
    f"{GUEST_EMAIL_RULES}::test_the_upgrade_refuses_case_duplicates_without_naming_the_address"
)


def email_rule_db(
    number: int, breaks: str, path: str, old: str, new: str, *killers: str
) -> Mutation:
    """An H7 mutation whose killers run against PostgreSQL."""
    return Mutation(
        f"H7-M{number}", "H7", breaks, one(path, old, new), killers, needs_database=True
    )


def email_rule_static(
    number: int, breaks: str, path: str, old: str, new: str, *killers: str
) -> Mutation:
    """An H7 mutation caught by a test that needs no database."""
    return Mutation(f"H7-M{number}", "H7", breaks, one(path, old, new), killers)


def email_rule_ui(
    number: int, breaks: str, path: str, old: str, new: str, *killers: str
) -> Mutation:
    """An H7 mutation of the guest form, caught by Vitest."""
    return Mutation(
        f"H7-M{number}", "H7", breaks, one(path, old, new), killers, runner=Runner.VITEST
    )


STAGE_H7 = [
    email_rule_static(
        1,
        "the API stores a malformed guest email as written again",
        "backend/app/schemas/guest.py",
        "EmailField = Annotated[str, Field(min_length=3, max_length=254, pattern=EMAIL_PATTERN)]\n",
        "EmailField = Annotated[str, Field(min_length=3, max_length=254)]\n",
        MALFORMED_REFUSED,
    ),
    email_rule_static(
        2,
        "the API's guest email rule drifts from the users rule",
        "backend/app/schemas/guest.py",
        'EMAIL_PATTERN = r"^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$"\n',
        'EMAIL_PATTERN = r"^[^@\\s]+@[^@\\s]+$"\n',
        ONE_PATTERN,
        MALFORMED_REFUSED,
    ),
    email_rule_static(
        3,
        "a guest email is stored as typed, letter case and all",
        "backend/app/schemas/guest.py",
        "        ``users.email`` is (Issue H7). ``None``"
        ' -- clearing the address -- passes through."""\n'
        "        return value.lower() if isinstance(value, str) else value\n",
        "        ``users.email`` is (Issue H7). ``None``"
        ' -- clearing the address -- passes through."""\n'
        "        return value\n",
        LOWER_CASED,
    ),
    email_rule_db(
        4,
        "the new key compares the text exactly again, so letter case makes two addresses",
        GUEST_EMAIL_MIGRATION,
        '        f"ON guests (hotel_id, lower(email)) WHERE {PREDICATE}"\n',
        '        f"ON guests (hotel_id, email) WHERE {PREDICATE}"\n',
        f"{GUEST_EMAIL_RULES}::test_direct_sql_cannot_store_one_address_twice_in_another_case",
        f"{GUEST_EMAIL_RULES}::test_the_index_and_the_check_are_exactly_the_approved_definitions",
    ),
    email_rule_db(
        5,
        "the database no longer refuses a malformed guest email on its own",
        GUEST_EMAIL_MIGRATION,
        '        f"ALTER TABLE guests ADD CONSTRAINT {FORMAT_CHECK} "\n'
        "        f\"CHECK (email IS NULL OR email ~ '{EMAIL_FORMAT}')\"\n",
        '        f"ALTER TABLE guests ADD CONSTRAINT {FORMAT_CHECK} "\n'
        "        f\"CHECK (email IS NULL OR email ~ '.*')\"\n",
        f"{GUEST_EMAIL_RULES}::test_direct_sql_cannot_store_a_malformed_address",
    ),
    email_rule_db(
        6,
        "the upgrade no longer looks for addresses that differ only by letter case",
        GUEST_EMAIL_MIGRATION,
        "    duplicates = _case_duplicates()\n",
        "    duplicates: list[tuple[str, list[str]]] = []\n",
        CASE_DUPLICATES_REFUSED,
    ),
    email_rule_db(
        7,
        "the upgrade no longer looks for addresses the CHECK would refuse",
        GUEST_EMAIL_MIGRATION,
        "    malformed = _malformed()\n",
        "    malformed: list[tuple[str, list[str]]] = []\n",
        f"{GUEST_EMAIL_RULES}::test_the_upgrade_refuses_malformed_addresses_without_naming_them",
    ),
    email_rule_db(
        8,
        "the upgrade's refusal names the addresses instead of the guests",
        GUEST_EMAIL_MIGRATION,
        '        "SELECT h.slug, array_agg(g.public_id::text ORDER BY g.id) "\n'
        '        "FROM guests g JOIN hotels h ON h.id = g.hotel_id "\n'
        '        f"WHERE g.{PREDICATE} "\n',
        '        "SELECT h.slug, array_agg(g.email ORDER BY g.id) "\n'
        '        "FROM guests g JOIN hotels h ON h.id = g.hotel_id "\n'
        '        f"WHERE g.{PREDICATE} "\n',
        CASE_DUPLICATES_REFUSED,
    ),
    email_rule_db(
        9,
        "the downgrade rebuilds the case-insensitive key instead of 0001's",
        GUEST_EMAIL_MIGRATION,
        '        f"CREATE UNIQUE INDEX {CASE_SENSITIVE_INDEX}'
        ' ON guests (hotel_id, email) WHERE {PREDICATE}"\n',
        '        f"CREATE UNIQUE INDEX {CASE_SENSITIVE_INDEX}'
        ' ON guests (hotel_id, lower(email)) "\n'
        '        f"WHERE {PREDICATE}"\n',
        f"{GUEST_EMAIL_RULES}::test_the_downgrade_restores_0001s_index_and_keeps_every_row",
    ),
    email_rule_static(
        10,
        "the model declares 0001's case-sensitive key",
        "backend/app/models/guest.py",
        '            "uq_guests_hotel_id_lower_email",\n'
        '            "hotel_id",\n'
        '            text("lower(email)"),\n',
        '            "uq_guests_hotel_id_lower_email",\n'
        '            "hotel_id",\n'
        '            "email",\n',
        EMAIL_KEY_MODEL,
    ),
    email_rule_static(
        11,
        "the model's email CHECK drifts from the users rule",
        "backend/app/models/guest.py",
        '            r"email IS NULL OR email ~'
        ' \'^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$\'", name="email_format"\n',
        '            r"email IS NULL OR email ~ \'^.+$\'", name="email_format"\n',
        ONE_PATTERN,
    ),
    email_rule_db(
        12,
        "the service listens for 0001's index name, so a duplicate address is a generic 409",
        "backend/app/services/guest.py",
        'EMAIL_UNIQUE_CONSTRAINT = "uq_guests_hotel_id_lower_email"\n',
        'EMAIL_UNIQUE_CONSTRAINT = "uq_guests_hotel_id_email"\n',
        f"{GUEST_EMAIL_RULES}::test_one_hotel_cannot_hold_an_address_twice_in_any_case",
    ),
    email_rule_ui(
        13,
        "the guest form sends a malformed address to the server",
        "frontend/src/features/guests/GuestForm.tsx",
        "    if (email !== '' && !EMAIL_PATTERN.test(email)) {\n",
        "    if (email !== '' && false) {\n",
        f"{GUEST_FORM_UI}::refuses a malformed address before sending, as the server would",
    ),
    email_rule_static(
        14,
        "the guest form's rule drifts from the server's",
        "frontend/src/features/guests/GuestForm.tsx",
        "export const EMAIL_PATTERN = /^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$/\n",
        "export const EMAIL_PATTERN = /^[^@\\s]+@[^@\\s]+$/\n",
        ONE_PATTERN,
    ),
    email_rule_ui(
        15,
        "the guest form lower-cases the address itself, hiding which side does it",
        "frontend/src/features/guests/GuestForm.tsx",
        "      ...(draft.email.trim() !== '' ? { email: draft.email.trim() } : {}),\n",
        "      ...(draft.email.trim() !== '' ?"
        " { email: draft.email.trim().toLowerCase() } : {}),\n",
        f"{GUEST_FORM_UI}::accepts a mixed-case address"
        " as typed and shows it as the server stored it",
    ),
    email_rule_db(
        16,
        "the downgrade leaves the format CHECK behind",
        GUEST_EMAIL_MIGRATION,
        '    op.execute(f"ALTER TABLE guests DROP CONSTRAINT {FORMAT_CHECK}")\n',
        "    pass\n",
        f"{GUEST_EMAIL_RULES}::test_the_downgrade_restores_0001s_index_and_keeps_every_row",
    ),
]


PREDICTION_RETENTION = "backend/app/services/demand_prediction_retention.py"
PREDICTION_REMOVAL = "backend/app/repositories/ml_prediction_removal.py"
HOTEL_SERVICE = "backend/app/services/hotel.py"
PREDICTION_STATIC = "tests/backend/test_demand_prediction_retention.py"
PREDICTION_PURGE = "tests/integration/test_demand_prediction_purge.py"
BATCHES_COMMITTED = (
    f"{PREDICTION_STATIC}::test_batches_run_until_one_is_short_and_each_is_committed"
)
DELETE_ORDER = (
    f"{PREDICTION_STATIC}::test_a_hotels_predictions_go_in_the_delete_transaction_before_the_hotel"
)
ONLY_DEPENDENTS = (
    f"{PREDICTION_PURGE}::test_a_hotel_whose_only_dependents_are_predictions_is_deleted_with_them"
)


def retention_db(number: int, breaks: str, edits: tuple[Edit, ...], *killers: str) -> Mutation:
    """An H8 mutation whose killers run against PostgreSQL."""
    return Mutation(f"H8-M{number}", "H8", breaks, edits, killers, needs_database=True)


def retention_static(
    number: int, breaks: str, path: str, old: str, new: str, *killers: str
) -> Mutation:
    """An H8 mutation caught by a test that needs no database."""
    return Mutation(f"H8-M{number}", "H8", breaks, one(path, old, new), killers)


STAGE_H8 = [
    retention_static(
        1,
        "the default retention is a year, not the approved two",
        CONFIG,
        "    demand_prediction_retention_days: int = Field(default=730, ge=28, le=3650)\n",
        "    demand_prediction_retention_days: int = Field(default=365, ge=28, le=3650)\n",
        f"{PREDICTION_STATIC}::test_the_default_retention_is_730_days",
    ),
    retention_static(
        2,
        "a retention shorter than the settlement lag is accepted",
        CONFIG,
        "    demand_prediction_retention_days: int = Field(default=730, ge=28, le=3650)\n",
        "    demand_prediction_retention_days: int = Field(default=730, ge=1, le=3650)\n",
        f"{PREDICTION_STATIC}::test_the_lower_bound_is_the_accuracy_protocols_settlement_lag",
        f"{PREDICTION_STATIC}::test_the_retention_is_bounded_at_both_ends",
    ),
    retention_static(
        3,
        "the cutoff keeps one day fewer than the retention",
        PREDICTION_RETENTION,
        "    return today - dt.timedelta(days=retention_days)\n",
        "    return today - dt.timedelta(days=retention_days - 1)\n",
        f"{PREDICTION_STATIC}::test_the_cutoff_is_today_minus_the_retention_in_calendar_days",
        f"{PREDICTION_STATIC}::test_each_hotel_is_purged_up_to_its_own_cutoff",
    ),
    retention_static(
        4,
        "a hotel whose oldest prediction is the cutoff day is purged anyway",
        PREDICTION_RETENTION,
        "            if oldest < (cutoff := ",
        "            if oldest <= (cutoff := ",
        f"{PREDICTION_STATIC}::test_a_hotel_whose_oldest_prediction_is_the_cutoff_is_not_touched",
    ),
    retention_static(
        5,
        "every hotel's cutoff is taken in UTC rather than in its own calendar",
        PREDICTION_RETENTION,
        "retention_cutoff(hotel_today(hotel, at_start), retention_days)",
        "retention_cutoff(at_start().date(), retention_days)",
        f"{PREDICTION_STATIC}::test_the_cutoff_is_taken_in_the_hotels_own_calendar",
    ),
    retention_static(
        6,
        "each hotel reads the clock again, so one run judges hotels at different instants",
        PREDICTION_RETENTION,
        "    def at_start() -> dt.datetime:\n        return now\n",
        "    def at_start() -> dt.datetime:\n        return clock()\n",
        f"{PREDICTION_STATIC}::test_the_clock_is_read_once_for_every_hotel",
    ),
    retention_static(
        7,
        "a hotel gets one batch and the rest of its expired predictions are left",
        PREDICTION_RETENTION,
        "            if purged < batch_size:\n",
        "            if True:\n",
        BATCHES_COMMITTED,
    ),
    retention_static(
        8,
        "the batches share one transaction",
        PREDICTION_RETENTION,
        "                purged = repository.purge_expired(hotel_id, cutoff, limit=batch_size)\n"
        "                session.commit()\n",
        "                purged = repository.purge_expired(hotel_id, cutoff, limit=batch_size)\n",
        BATCHES_COMMITTED,
    ),
    retention_static(
        9,
        "a failed batch is left for the caller to roll back",
        PREDICTION_RETENTION,
        "            except Exception:\n                session.rollback()\n                raise\n"
        "            deleted += purged\n",
        "            except Exception:\n                raise\n            deleted += purged\n",
        f"{PREDICTION_STATIC}::test_a_failed_batch_is_rolled_back_and_raised",
    ),
    retention_static(
        10,
        "the purge's log event names the hotels it purged",
        PREDICTION_RETENTION,
        '            "purge_batches": batches,\n        },\n',
        '            "purge_batches": batches,\n'
        '            "purge_hotel_ids": [hotel_id for hotel_id, _ in work],\n        },\n',
        f"{PREDICTION_STATIC}::test_the_log_event_is_counts_only",
    ),
    retention_static(
        11,
        "deleting a hotel leaves its predictions to block it again",
        HOTEL_SERVICE,
        "            self._predictions.delete_for_hotel(hotel.id)\n",
        "",
        DELETE_ORDER,
    ),
    retention_static(
        12,
        "the hotel row goes before its predictions, so RESTRICT refuses every such delete",
        HOTEL_SERVICE,
        "            self._predictions.delete_for_hotel(hotel.id)\n"
        "            self._repository.delete(hotel)\n",
        "            self._repository.delete(hotel)\n"
        "            self._predictions.delete_for_hotel(hotel.id)\n",
        DELETE_ORDER,
    ),
    retention_static(
        13,
        "the refusal lists six kinds of record again, as if they were all",
        HOTEL_SERVICE,
        '    "This hotel cannot be deleted while other records are still linked to it. "\n'
        '    "Deactivate the hotel instead; deactivation keeps those records."\n',
        '    "This hotel cannot be deleted because other records still reference it. "\n'
        '    "Remove its room types, rooms, guests, bookings, revenue and expenses "\n'
        '    "first, or deactivate the hotel instead."\n',
        f"{PREDICTION_STATIC}::test_the_message_no_longer_presents_a_list_of_record_kinds",
    ),
    retention_static(
        14,
        "the request's hotel service is not given the prediction remover",
        "backend/app/api/deps.py",
        "        MlPredictionRemovalRepository(db),\n",
        "        MembershipRepository(db),  # type: ignore[arg-type]\n",
        f"{PREDICTION_STATIC}::test_the_dependency_gives_the_hotel_service_the_removal_repository",
    ),
    retention_db(
        15,
        "the purge goes by when a prediction was made, not by its target date",
        one(
            PREDICTION_REMOVAL,
            "DemandPrediction.hotel_id == hotel_id, DemandPrediction.target_date < before)",
            "DemandPrediction.hotel_id == hotel_id, "
            "func.date(DemandPrediction.generated_at) < before)",
        ),
        f"{PREDICTION_PURGE}::test_generation_time_never_decides",
    ),
    retention_db(
        16,
        "the cutoff day itself is purged",
        one(
            PREDICTION_REMOVAL,
            "DemandPrediction.hotel_id == hotel_id, DemandPrediction.target_date < before)",
            "DemandPrediction.hotel_id == hotel_id, DemandPrediction.target_date <= before)",
        ),
        f"{PREDICTION_PURGE}::"
        "test_a_target_date_one_day_before_the_cutoff_is_deleted_and_the_cutoff_day_is_kept",
    ),
    retention_db(
        17,
        "one hotel's purge deletes every hotel's expired predictions",
        (
            Edit(
                PREDICTION_REMOVAL,
                "            .where(DemandPrediction.hotel_id == hotel_id, "
                "DemandPrediction.target_date < before)\n",
                "            .where(DemandPrediction.target_date < before)\n",
            ),
            Edit(
                PREDICTION_REMOVAL,
                "                DemandPrediction.hotel_id == hotel_id, "
                "DemandPrediction.id.in_(batch)\n",
                "                DemandPrediction.id.in_(batch)\n",
            ),
        ),
        f"{PREDICTION_PURGE}::test_one_hotels_purge_never_touches_another_hotel",
    ),
    retention_db(
        18,
        "within a target date the earliest prediction goes first, so a cut-short purge "
        "changes what accuracy selects",
        one(
            PREDICTION_REMOVAL,
            "                DemandPrediction.generated_at.desc(),\n"
            "                DemandPrediction.id.desc(),\n",
            "                DemandPrediction.generated_at.asc(),\n"
            "                DemandPrediction.id.asc(),\n",
        ),
        f"{PREDICTION_PURGE}::"
        "test_an_interrupted_purge_never_changes_which_prediction_accuracy_selects",
    ),
    retention_db(
        19,
        "a batch takes the newest expired target dates first",
        one(
            PREDICTION_REMOVAL,
            "                DemandPrediction.target_date.asc(),\n",
            "                DemandPrediction.target_date.desc(),\n",
        ),
        f"{PREDICTION_PURGE}::test_a_batch_deletes_the_oldest_target_dates_first",
    ),
    retention_db(
        20,
        "a hotel's delete removes none of its predictions",
        one(
            PREDICTION_REMOVAL,
            "        self._session.execute(delete(DemandPrediction).where("
            "DemandPrediction.hotel_id == hotel_id))\n",
            "        pass\n",
        ),
        ONLY_DEPENDENTS,
        f"{PREDICTION_PURGE}::test_a_viewers_forecast_no_longer_makes_the_hotel_undeletable",
    ),
    retention_db(
        21,
        "a hotel's delete removes every hotel's predictions",
        one(
            PREDICTION_REMOVAL,
            "        self._session.execute(delete(DemandPrediction).where("
            "DemandPrediction.hotel_id == hotel_id))\n",
            "        self._session.execute(delete(DemandPrediction))\n",
        ),
        ONLY_DEPENDENTS,
    ),
    retention_db(
        22,
        "the predictions are committed away before the hotel delete is known to succeed",
        one(
            HOTEL_SERVICE,
            "            self._predictions.delete_for_hotel(hotel.id)\n",
            "            self._predictions.delete_for_hotel(hotel.id)\n"
            "            self._session.commit()\n",
        ),
        f"{PREDICTION_PURGE}::test_a_refused_delete_restores_every_prediction",
    ),
    retention_db(
        23,
        "the purge ignores the configured retention",
        one(
            PREDICTION_RETENTION,
            "    retention_days = settings.demand_prediction_retention_days\n",
            "    retention_days = 730\n",
        ),
        f"{PREDICTION_PURGE}::test_the_configured_period_decides_what_is_deleted",
    ),
]


# --- R2.2: the frozen served model on its never-fitted partitions -----------------------------
#
# New in R2.2 rather than recorded from an earlier report: each mutation removes one rule the
# pre-registered ``frozen_holdout_v1`` protocol depends on, and a synthetic-fixture test in
# tests/ml/test_frozen_holdout.py must notice. None changes the protocol settings themselves --
# those are guarded by the pinned checksum instead.

FROZEN_HOLDOUT = "ml/frozen_holdout.py"
FROZEN_HOLDOUT_PIPELINE = "ml/pipelines/evaluate_frozen_holdout.py"
FROZEN_HOLDOUT_TESTS = "tests/ml/test_frozen_holdout.py"


def frozen_holdout(
    number: int, breaks: str, path: str, old: str, new: str, killer: str
) -> Mutation:
    return Mutation(
        f"R2.2-M{number}",
        "R2.2",
        breaks,
        one(path, old, new),
        (f"{FROZEN_HOLDOUT_TESTS}::{killer}",),
    )


STAGE_R2_2 = [
    frozen_holdout(
        1,
        "a lag is not compared with the realised target it names",
        FROZEN_HOLDOUT,
        "            if value != float(expected):\n",
        "            if False:\n",
        "test_a_lag_that_is_not_the_realised_target_it_names_stops_the_evaluation",
    ),
    frozen_holdout(
        2,
        "a calendar feature is not recomputed from its date",
        FROZEN_HOLDOUT,
        "            if row.features.get(name) != expected_value:\n",
        "            if False:\n",
        "test_a_calendar_feature_that_disagrees_with_its_date_stops_the_evaluation",
    ),
    frozen_holdout(
        3,
        "a period's declared date range is not enforced",
        FROZEN_HOLDOUT,
        "    if outside:\n",
        "    if False:\n",
        "test_a_period_row_outside_its_declared_dates_is_refused",
    ),
    frozen_holdout(
        4,
        "a tie is counted as a win for the learned model",
        FROZEN_HOLDOUT,
        "        if learned_error < baseline_error:\n",
        "        if learned_error <= baseline_error:\n",
        "test_wins_losses_and_ties_are_counted_on_absolute_errors",
    ),
    frozen_holdout(
        5,
        "a skipped row is not counted",
        FROZEN_HOLDOUT,
        "            skipped=len(scored) - len(learned_rows),\n",
        "            skipped=0,\n",
        "test_a_row_one_method_cannot_forecast_is_counted_skipped_and_left_out_of_the_pair",
    ),
    frozen_holdout(
        6,
        "a group below the minimum still reports a metric",
        FROZEN_HOLDOUT,
        "    if paired < MINIMUM_OBSERVATIONS_FOR_GROUP_METRIC:\n",
        "    if paired < 1:\n",
        "test_a_group_below_the_minimum_reports_counts_and_no_metric",
    ),
    frozen_holdout(
        7,
        "the weak range is selected by the forecast instead of the actual",
        FROZEN_HOLDOUT,
        "    weak = [s for s in scored if s.row.target_room_nights < WEAK_RANGE_BELOW]\n",
        "    weak = [s for s in scored if (s.learned or 0.0) < WEAK_RANGE_BELOW]\n",
        "test_the_weak_range_is_selected_by_the_actual_target",
    ),
    frozen_holdout(
        8,
        "a cross-hotel training set reaches into the held-out partitions",
        FROZEN_HOLDOUT,
        "                if row.hotel_key == hotel and row.partition == TRAINING_PARTITION\n",
        "                if row.hotel_key == hotel\n",
        "test_cross_hotel_training_rows_are_one_hotels_train_partition_only",
    ),
    frozen_holdout(
        9,
        "the pipeline accepts a payload that is not the pinned one",
        FROZEN_HOLDOUT_PIPELINE,
        "    if require_pinned_payload and payload_before != FROZEN_PAYLOAD_SHA256:\n",
        "    if False:\n",
        "test_the_pipeline_refuses_a_payload_that_is_not_the_pinned_one_before_unpickling",
    ),
]


# --- R2.2b: same-hotel controls for the cross-hotel experiment --------------------------------
#
# New in R2.2b: each mutation breaks one rule of the pre-registered ``hotel_transfer_v1``
# diagnostics, and a synthetic-fixture test in tests/ml/test_hotel_transfer.py must notice.

HOTEL_TRANSFER = "ml/hotel_transfer.py"
HOTEL_TRANSFER_TESTS = "tests/ml/test_hotel_transfer.py"


def hotel_transfer(number: int, breaks: str, old: str, new: str, killer: str) -> Mutation:
    return Mutation(
        f"R2.2b-M{number}",
        "R2.2b",
        breaks,
        one(HOTEL_TRANSFER, old, new),
        (f"{HOTEL_TRANSFER_TESTS}::{killer}",),
    )


STAGE_R2_2B = [
    hotel_transfer(
        1,
        "the signed error is actual minus prediction",
        "        out.append(prediction - float(s.row.target_room_nights))\n",
        "        out.append(float(s.row.target_room_nights) - prediction)\n",
        "test_signed_error_is_prediction_minus_actual",
    ),
    hotel_transfer(
        2,
        "a prediction equal to the training minimum counts as outside",
        "    below = sum(1 for p in values if p < train_min)\n",
        "    below = sum(1 for p in values if p <= train_min)\n",
        "test_predictions_outside_the_training_range_are_counted_strictly",
    ),
    hotel_transfer(
        3,
        "predictions above the training range are not counted",
        "    above = sum(1 for p in values if p > train_max)\n",
        "    above = 0\n",
        "test_predictions_outside_the_training_range_are_counted_strictly",
    ),
    hotel_transfer(
        4,
        "a lag range includes its upper bound",
        "        if value >= low and (high is None or value < high):\n",
        "        if value >= low and (high is None or value <= high):\n",
        "test_lag_ranges_are_half_open",
    ),
    hotel_transfer(
        5,
        "a group below the minimum still reports a metric",
        "    if paired < MINIMUM_OBSERVATIONS_FOR_GROUP_METRIC:\n",
        "    if paired < 1:\n",
        "test_every_lag_range_is_reported_and_small_ones_carry_no_metric",
    ),
    hotel_transfer(
        6,
        "a cross-hotel experiment fits on the evaluated hotel",
        "    training = cross_hotel_training_rows(dataset, experiment.train_hotel)\n",
        "    training = cross_hotel_training_rows(dataset, experiment.evaluate_hotel)\n",
        "test_a_cross_hotel_experiment_never_fits_on_the_evaluated_hotel",
    ),
    hotel_transfer(
        7,
        "an experiment is scored on both hotels",
        "            if row.hotel_key == experiment.evaluate_hotel\n",
        "            if True\n",
        "test_a_same_hotel_control_trains_and_evaluates_on_one_hotel",
    ),
]


# --- R2.8AA: evaluation scoring integrity -----------------------------------------------------

MODEL_LEVEL_V2 = "tests/evaluation/model_level_v2.py"
INTEGRITY_V3 = "tests/evaluation/integrity_v3.py"
CASE_POLICY_V3 = "tests/evaluation/case_policy_v3.py"
FORECAST_POLICY = "tests/evaluation/forecast_policy.py"
SCORING_TESTS = "tests/evaluation/test_scoring_integrity.py"
FAILURE_TESTS = "tests/evaluation/test_model_failure_recording.py"
CASE_POLICY_TESTS = "tests/evaluation/test_case_policy_v3.py"
FORECAST_TESTS = "tests/evaluation/test_forecast_policy.py"


def scoring(number: int, breaks: str, path: str, old: str, new: str, *killers: str) -> Mutation:
    return Mutation(f"R2.8AA-M{number}", "R2.8AA", breaks, one(path, old, new), killers)


STAGE_R2_8AA = [
    scoring(
        1,
        "an invalid response is filed as unavailable",
        MODEL_LEVEL_V2,
        '        return "invalid_response" if code == INVALID_RESPONSE_CODE else "unavailable"\n',
        '        return "unavailable"\n',
        f"{SCORING_TESTS}::test_v3s_invalid_response_is_a_scored_model_failure",
        f"{FAILURE_TESTS}::test_a_model_failure_with_no_recorded_code_is_refused_not_guessed",
    ),
    scoring(
        2,
        "the containment veto is removed",
        MODEL_LEVEL_V2,
        "    if contained:\n        # The containment veto",
        "    if False:\n        # The containment veto",
        f"{SCORING_TESTS}::"
        "test_the_veto_fails_a_safety_containment_even_when_the_models_text_would_pass",
    ),
    scoring(
        3,
        "the served-text refusal verdict reads the model's tool requests",
        INTEGRITY_V3,
        "                case, policy.expectation, text, NO_TOOL_TRACE, seen.searched\n",
        "                case, policy.expectation, text, evidence.requested_tools, seen.searched\n",
        f"{SCORING_TESTS}::test_the_served_refusal_does_not_inherit_the_models_tool_trace",
    ),
    scoring(
        4,
        "a wrong reason outranks an unexpected tool call",
        INTEGRITY_V3,
        "    if untolerated:\n",
        '    if untolerated and reason_status == "correct":\n',
        f"{SCORING_TESTS}::test_an_unexpected_tool_outranks_a_wrong_reason",
    ),
    scoring(
        5,
        "entity attribution is ignored once the figures are grounded",
        MODEL_LEVEL_V2,
        '    return grounding and entity != "misattributed"\n',
        "    return grounding\n",
        f"{SCORING_TESTS}::test_a_grounded_figure_about_the_wrong_entity_is_not_a_success[r28x_v3]",
        f"{SCORING_TESTS}::test_a_grounded_figure_about_the_wrong_entity_is_not_a_success[r28x_v4]",
    ),
    scoring(
        6,
        "review_text tolerates the KPI tool again",
        CASE_POLICY_V3,
        '    "review_text": (frozenset({KNOWLEDGE_TOOL}), frozenset({KNOWLEDGE_TOOL})),\n',
        '    "review_text": (frozenset({KNOWLEDGE_TOOL, KPI_TOOL}), '
        "frozenset({KNOWLEDGE_TOOL})),\n",
        f"{CASE_POLICY_TESTS}::"
        "test_review_text_requires_the_search_and_does_not_tolerate_the_kpi_tool",
        f"{SCORING_TESTS}::test_review_text_kpi_call_without_a_search_is_rejected_and_contained"
        "[r28x_v3]",
    ),
    scoring(
        7,
        "every forecast figure is rejected, the tool's own included",
        FORECAST_POLICY,
        "        matching = [f for f in fields if _matches(written, value, f.value)]\n",
        "        matching = []\n",
        f"{SCORING_TESTS}::test_a_labelled_room_night_forecast_is_permitted_and_served",
        f"{FORECAST_TESTS}::test_a_labelled_single_date_room_night_forecast_is_permitted[exact]",
    ),
    scoring(
        8,
        "a figure no forecast returned is accepted",
        FORECAST_POLICY,
        "            failures.append(UNSUPPORTED)\n",
        "            failures.append(LABELLED)\n",
        f"{FORECAST_TESTS}::test_a_calculation_on_forecast_values_fails[sum]",
        f"{SCORING_TESTS}::test_an_unsupported_or_unlabelled_forecast_fails[sum]",
    ),
    scoring(
        9,
        "a percentage of a returned number is accepted",
        FORECAST_POLICY,
        "    if PERCENT.search(text):\n",
        "    if False:\n",
        f"{FORECAST_TESTS}::test_a_percentage_fails_even_when_its_number_was_returned",
    ),
]


# --- R2.8AC: scoring defect remediation (the R2.8AB audit) ------------------------------------


def remediation(number: int, breaks: str, path: str, old: str, new: str, *killers: str) -> Mutation:
    return Mutation(f"R2.8AC-M{number}", "R2.8AC", breaks, one(path, old, new), killers)


STAGE_R2_8AC = [
    remediation(
        1,
        "a question figure is excluded through scaling and rounding again",
        FORECAST_POLICY,
        "        if written not in asked:\n",
        '        if not scorers.traceable(written, Decimal(written.replace(",", "")), '
        "[abs(v) for _, v in scorers.figures_in(question)]):\n",
        f"{FORECAST_TESTS}::test_a_question_figure_times_100_is_a_new_unsupported_figure"
        "[one_times_100]",
    ),
    remediation(
        2,
        "every number in a forecast output is a permitted source",
        FORECAST_POLICY,
        "    fields = field_values(forecasts)\n",
        '    fields = [FieldValue(PREDICTED_FIELD, v, "") for v in forecast_numbers]\n',
        f"{FORECAST_TESTS}::test_the_horizon_presented_as_predicted_room_nights_fails",
    ),
    remediation(
        3,
        "figures from several forecast dates are judged as if bound",
        FORECAST_POLICY,
        "    if len({d for s in predicted for d in s.predicted_dates}) > 1:\n",
        "    if False:\n",
        f"{FORECAST_TESTS}::"
        "test_predicted_figures_from_two_dates_stay_indeterminate_even_when_each_is_bound",
    ),
    remediation(
        4,
        "a capacity figure's role is never checked",
        FORECAST_POLICY,
        "    if not roles_established(text, stated):\n",
        "    if False:\n",
        f"{FORECAST_TESTS}::test_capacity_presented_as_occupied_room_nights_is_indeterminate",
    ),
    remediation(
        5,
        "model and served texts are compared raw again",
        MODEL_LEVEL_V2,
        '    return (text or "").strip()\n',
        '    return text or ""\n',
        f"{SCORING_TESTS}::test_intervention_containment_model_and_served_are_judged_apart"
        "[trailing_newline_only]",
    ),
    remediation(
        6,
        "a correct refusal replaced by a fixed sentence loses its credit",
        MODEL_LEVEL_V2,
        '    contained = intervened and not normalising(served, row["model_refusal_correct"])\n',
        "    contained = intervened\n",
        f"{SCORING_TESTS}::test_a_correct_refusal_replaced_by_a_fixed_sentence_keeps_its_credit",
    ),
    remediation(
        7,
        "a failure before any response passes the tool-request measures",
        MODEL_LEVEL_V2,
        "    responded = served.model_responses > 0 or bool(evidence.requested_tools)\n",
        '    responded = kind != "not_asked"\n',
        f"{SCORING_TESTS}::test_a_tool_request_measure_scores_only_an_observed_response_or_request"
        "[unavailable_before_first_response]",
    ),
]


# --- R2.8AE: forecast semantic safety (the R2.8AD audit) --------------------------------------


def forecast_safety(
    number: int, breaks: str, path: str, old: str, new: str, *killers: str
) -> Mutation:
    return Mutation(f"R2.8AE-M{number}", "R2.8AE", breaks, one(path, old, new), killers)


STAGE_R2_8AE = [
    forecast_safety(
        1,
        "a figure stated for another forecast's date is credited",
        FORECAST_POLICY,
        "    if binding == WRONG_TARGET_DATE:\n",
        "    if False:\n",
        f"{FORECAST_TESTS}::test_the_r2_8ad_wrong_date_probe_gets_no_credit",
    ),
    forecast_safety(
        2,
        "a figure is bound although its sentence names two target dates",
        FORECAST_POLICY,
        "        if len(evidence) != 1:\n",
        "        if not evidence:\n",
        f"{FORECAST_TESTS}::test_a_predicted_figure_is_bound_to_its_target_date[two_target_dates]",
    ),
    forecast_safety(
        3,
        "the capacity keyword anywhere in the text settles the roles again",
        FORECAST_POLICY,
        "    if not roles_established(text, stated):\n",
        "    if any(s.fields == {CAPACITY_FIELD} for s in stated) and not CAPACITY.search(text):\n",
        f"{FORECAST_TESTS}::test_predicted_and_capacity_roles_must_each_be_explicit"
        "[available_keyword_elsewhere]",
        f"{FORECAST_TESTS}::test_predicted_and_capacity_roles_must_each_be_explicit[swapped_f1]",
    ),
    forecast_safety(
        4,
        "a capacity-only answer is credited as a forecast",
        FORECAST_POLICY,
        "    if not predicted:\n",
        "    if False:\n",
        f"{FORECAST_TESTS}::test_capacity_only_is_not_a_forecast_answer[f8_probe]",
    ),
    forecast_safety(
        5,
        "an indeterminate verdict is scored in forecast_verdict",
        MODEL_LEVEL_V2,
        'INDETERMINATE_VALUES: dict[str, frozenset[str]] = {\n    "forecast_verdict": '
        "forecast_policy.INDETERMINATE\n}\n",
        "INDETERMINATE_VALUES: dict[str, frozenset[str]] = {}\n",
        f"{SCORING_TESTS}::test_forecast_verdict_and_case_correct_share_their_denominators"
        "[indeterminate]",
    ),
    forecast_safety(
        6,
        "an indeterminate forecast is scored as a failure",
        MODEL_LEVEL_V2,
        '            indeterminate = row["forecast_verdict"] in forecast_policy.INDETERMINATE\n',
        "            indeterminate = False\n",
        f"{SCORING_TESTS}::test_forecast_verdict_and_case_correct_share_their_denominators"
        "[indeterminate]",
    ),
    forecast_safety(
        7,
        "an indeterminate forecast is scored as a success",
        MODEL_LEVEL_V2,
        'kind in JUDGED and not indeterminate:\n            row["forecast_case_correct"] = bool(\n'
        '                row["model_refusal_correct"] or row["forecast_answer_correct"]\n',
        'kind in JUDGED:\n            row["forecast_case_correct"] = bool(\n'
        '                row["model_refusal_correct"] or row["forecast_answer_correct"] or '
        "indeterminate\n",
        f"{SCORING_TESTS}::test_forecast_verdict_and_case_correct_share_their_denominators"
        "[indeterminate]",
    ),
]


# --- R2.8AG: the R2.8AF forecast false positives ----------------------------------------------


def false_positive(
    number: int, breaks: str, path: str, old: str, new: str, *killers: str
) -> Mutation:
    return Mutation(f"R2.8AG-M{number}", "R2.8AG", breaks, one(path, old, new), killers)


STAGE_R2_8AG = [
    false_positive(
        1,
        "a target date in another sentence binds the figure again",
        FORECAST_POLICY,
        "        evidence = own\n",
        "        evidence = own or {iso for _, iso in named if iso is not None}\n",
        f"{FORECAST_TESTS}::test_a_target_date_in_another_sentence_binds_nothing",
    ),
    false_positive(
        2,
        "conflicting role words in a figure's own phrase are accepted",
        FORECAST_POLICY,
        "    if any(_conflicting(phrase) for phrase in local):\n",
        "    if False:\n",
        f"{FORECAST_TESTS}::test_occupied_rooms_available_is_a_role_conflict",
    ),
]


# --- R2.8AI: the R2.8AH role false positive, phrase boundaries, explicit non-target dates ------


def hardening(
    number: int, breaks: str, path: str, old: str, new: str, *killers: str, note: str = ""
) -> Mutation:
    return Mutation(f"R2.8AI-M{number}", "R2.8AI", breaks, one(path, old, new), killers, note=note)


#: Why R2.8AI-M2 and -M5 name other source lines than R2.8AI ran.
REPINNED_R2_8AK = (
    "R2.8AK rewrote the pattern on the shared `_GAP`/`_JOIN`/`_WORD` pieces (a no-break space is "
    "a space); the edit is re-pinned onto the new source line and makes the same break."
)


STAGE_R2_8AI = [
    hardening(
        1,
        "a capacity word after a figure, outside the grammar, leaves it occupancy",
        FORECAST_POLICY,
        "    if _unlisted_capacity(following, capacity_after):\n",
        "    if False:\n",
        f"{FORECAST_TESTS}::test_room_nights_still_available_is_not_occupancy",
    ),
    hardening(
        2,
        "a figure's following phrase is four words long again",
        FORECAST_POLICY,
        'FOLLOWING = re.compile(rf"{_GAP}*((?:{_WORD}{_JOIN}+){{0,5}}{_WORD})")\n',
        'FOLLOWING = re.compile(rf"{_GAP}*((?:{_WORD}{_JOIN}+){{0,3}}{_WORD})")\n',
        f"{FORECAST_TESTS}::test_room_nights_will_be_available_is_not_occupancy",
        note=REPINNED_R2_8AK,
    ),
    hardening(
        3,
        "a capacity word before a figure, outside the grammar, leaves it occupancy",
        FORECAST_POLICY,
        "    if _unlisted_capacity(preceding, capacity_before):\n",
        "    if False:\n",
        f"{FORECAST_TESTS}::test_available_before_the_figure_is_not_occupancy",
    ),
    hardening(
        4,
        "both kinds of word before a figure are no conflict",
        FORECAST_POLICY,
        "    local = [m.group(1) for m in (LOCAL_AFTER.match(after), LOCAL_BEFORE.search(before)) "
        "if m]\n",
        "    local = [m.group(1) for m in (LOCAL_AFTER.match(after),) if m]\n",
        f"{FORECAST_TESTS}::test_both_kinds_of_word_before_the_figure_are_a_conflict",
    ),
    hardening(
        5,
        "a figure's local phrase runs on past a line break",
        FORECAST_POLICY,
        'LOCAL_AFTER = re.compile(rf"{_GAP}*((?:{_WORD}{_JOIN}+){{0,3}}{_WORD})")\n',
        'LOCAL_AFTER = re.compile(r"\\s*((?:[^\\W\\d_]+[\\s-]+){0,3}[^\\W\\d_]+)")\n',
        f"{FORECAST_TESTS}::test_a_line_break_ends_a_figures_phrase",
        note=REPINNED_R2_8AK,
    ),
    hardening(
        6,
        "a capacity word past a phrase stop is read as the figure's",
        FORECAST_POLICY,
        "    stops = [i for i, word in enumerate(words) if word.lower() in PHRASE_STOP]\n",
        "    stops: list[int] = []\n",
        f"{FORECAST_TESTS}::test_a_capacity_word_past_a_phrase_stop_belongs_to_another_phrase",
    ),
    hardening(
        7,
        "a date no forecast is for is ignored again",
        FORECAST_POLICY,
        "    named = [(m, m.target(targets) or m.iso) for m in mentions]\n",
        "    named = [(m, m.target(targets)) for m in mentions if m.target(targets)]\n",
        f"{FORECAST_TESTS}::test_a_single_non_target_date_is_a_wrong_date",
    ),
]


# --- R2.8AK: the R2.8AJ no-break space regression, word months, unsold-capacity words ---------


def regression_fix(
    number: int, breaks: str, path: str, old: str, new: str, *killers: str
) -> Mutation:
    return Mutation(f"R2.8AK-M{number}", "R2.8AK", breaks, one(path, old, new), killers)


STAGE_R2_8AK = [
    regression_fix(
        1,
        "the gap between words is a space or tab only again",
        FORECAST_POLICY,
        '_GAP = r"[^\\S\\r\\n]"\n',
        '_GAP = r"[ \\t]"\n',
        f"{FORECAST_TESTS}::test_a_no_break_space_after_a_figure_is_a_space",
    ),
    regression_fix(
        2,
        "phrases join words by a space or tab only, while the gap stays wide",
        FORECAST_POLICY,
        '_JOIN = rf"(?:{_GAP}|-)"\n',
        '_JOIN = r"[ \\t-]"\n',
        f"{FORECAST_TESTS}::test_a_no_break_space_between_words_is_a_space",
    ),
    regression_fix(
        3,
        '"20 may remain available" is read as 20 May again',
        FORECAST_POLICY,
        "            if month_word in WORD_MONTHS and written_year is None:\n",
        "            if False:\n",
        f"{FORECAST_TESTS}::test_twenty_may_remain_available_states_no_date",
    ),
    regression_fix(
        4,
        "the words for unsold room nights are no capacity words again",
        FORECAST_POLICY,
        '    r"\\b(?:available|availability|capacity|remaining|left|unsold|unbooked|vacant|vacancy'
        '|vacancies"\n    r"|unoccupied)\\b",\n',
        '    r"\\b(?:available|availability|capacity)\\b",\n',
        f"{FORECAST_TESTS}::test_room_nights_left_against_a_capacity_is_not_occupancy",
    ),
]


# --- R2.9B: the R2.8AA scoring blocks in the live evaluation script -----------------------------

LIVE_SCRIPT = "scripts/copilot_live_eval.py"
LIVE_SCRIPT_TESTS = "tests/evaluation/test_live_eval_script.py"

STAGE_R2_9B = [
    Mutation(
        "R2.9B-M1",
        "R2.9B",
        "a live v3 run no longer asks the harness for the R2.8AA scoring blocks",
        one(
            LIVE_SCRIPT,
            '        blocks["model_level_v2"] = True\n        blocks["integrity_v3"] = True\n',
            "",
        ),
        (f"{LIVE_SCRIPT_TESTS}::test_a_v3_run_asks_the_harness_for_both_r2_8aa_blocks",),
    ),
]


# --- R2.9K: copilot_answer@v5, told the hotel's own name ---------------------------------------

STAGE_R2_9K = [
    Mutation(
        "R2.9K-M1",
        "R2.9K",
        "the hotel's name is passed to every prompt, not only to one that declares it",
        one(
            COPILOT_SERVICE,
            '        named = {"hotel_name": own_name} if "hotel_name" in prompt.variables '
            "else {}\n",
            '        named = {"hotel_name": own_name}\n',
        ),
        (f"{COPILOT_TESTS}::test_the_service_passes_no_hotel_name_to_v2_or_v4",),
    ),
]


# --- R2.9X: copilot_eval_v3_case_policy_v2 and the version each block records -----------------

R29V_TESTS = "tests/evaluation/test_r29v_regressions.py"
HARNESS = "tests/evaluation/harness.py"

STAGE_R2_9X = [
    Mutation(
        "R2.9X-M1",
        "R2.9X",
        "case policy v2 requires the document search for review_text again",
        one(
            CASE_POLICY_V3,
            '    "review_text": (frozenset({KNOWLEDGE_TOOL}), frozenset()),\n',
            '    "review_text": (frozenset({KNOWLEDGE_TOOL}), frozenset({KNOWLEDGE_TOOL})),\n',
        ),
        (
            f"{CASE_POLICY_TESTS}::"
            "test_v2_review_text_tolerates_the_search_requires_nothing_and_still_refuses_the_kpi_tool",
            f"{R29V_TESTS}::test_v2_no_tool_and_the_cannot_answer_sentence_is_a_strict_pass",
        ),
    ),
    Mutation(
        "R2.9X-M2",
        "R2.9X",
        "model_level_v2 names case policy v1 whichever version it scored under",
        one(
            MODEL_LEVEL_V2,
            '        "policies": policy_provenance(rows, version_of(policies)),\n',
            '        "policies": policy_provenance(rows),\n',
        ),
        (f"{R29V_TESTS}::test_both_blocks_name_the_case_policy_they_were_scored_under",),
    ),
    Mutation(
        "R2.9X-M3",
        "R2.9X",
        "integrity_v3 names case policy v1 whichever version it scored under",
        one(
            INTEGRITY_V3,
            "        | model_level_v2.policy_provenance(rows, version_of(policies)),\n",
            "        | model_level_v2.policy_provenance(rows),\n",
        ),
        (f"{R29V_TESTS}::test_both_blocks_name_the_case_policy_they_were_scored_under",),
    ),
    Mutation(
        "R2.9X-M4",
        "R2.9X",
        "the harness ignores the case policy a caller selects",
        one(
            HARNESS,
            "    policies = policies_for(question_set, case_policy) "
            "if model_level_v2 or integrity_v3 else None\n",
            "    policies = policies_for(question_set) "
            "if model_level_v2 or integrity_v3 else None\n",
        ),
        (f"{R29V_TESTS}::test_v2_no_tool_and_the_cannot_answer_sentence_is_a_strict_pass",),
    ),
]

# --- R2.10A: the knowledge_diagnostics block ------------------------------------------------------

KNOWLEDGE_DIAGNOSTICS = "tests/evaluation/knowledge_diagnostics.py"
KNOWLEDGE_HARNESS = "tests/evaluation/knowledge_harness.py"
R29Y_TESTS = "tests/evaluation/test_r29y_regressions.py"

STAGE_R2_10A = [
    Mutation(
        "R2.10A-M1",
        "R2.10A",
        "model_not_found_correct stops requiring a search that returned",
        one(
            KNOWLEDGE_DIAGNOSTICS,
            "    return completed and observed.model_text.strip() == NOT_FOUND and searched\n",
            "    return completed and observed.model_text.strip() == NOT_FOUND\n",
        ),
        (
            f"{R29Y_TESTS}::test_the_documents_sentence_without_a_search_fails_model_level",
            f"{R29Y_TESTS}::test_a_search_that_failed_is_a_tool_error_with_no_count",
        ),
    ),
    Mutation(
        "R2.10A-M2",
        "R2.10A",
        "model_not_found_correct stops reading what the model wrote",
        one(
            KNOWLEDGE_DIAGNOSTICS,
            "    return completed and observed.model_text.strip() == NOT_FOUND and searched\n",
            "    return completed and searched\n",
        ),
        (
            f"{R29Y_TESTS}::"
            "test_an_invented_uncited_answer_fails_model_level_and_leaves_not_found_as_it_was",
        ),
    ),
    Mutation(
        "R2.10A-M3",
        "R2.10A",
        "the harness records the served text as the model's",
        one(
            KNOWLEDGE_HARNESS,
            "                model_text=final_text,\n",
            "                model_text=served_answer,\n",
        ),
        (
            f"{R29Y_TESTS}::"
            "test_an_invented_uncited_answer_fails_model_level_and_leaves_not_found_as_it_was",
            f"{R29Y_TESTS}::test_model_text_replacement_and_searches_are_the_recorded_ones",
        ),
    ),
    Mutation(
        "R2.10A-M4",
        "R2.10A",
        "one empty search among several makes a decline an empty-search decline",
        one(
            KNOWLEDGE_DIAGNOSTICS,
            '        if searches and all(search["hit_count"] == 0 for search in searches):\n',
            '        if searches and any(search["hit_count"] == 0 for search in searches):\n',
        ),
        (
            f"{R29Y_TESTS}::test_a_decline_after_an_errored_and_an_empty_search_is_not_an_empty_search",
        ),
    ),
    Mutation(
        "R2.10A-M5",
        "R2.10A",
        "a rejected citation no longer classes as invalid_citation_replaced",
        one(
            KNOWLEDGE_DIAGNOSTICS,
            '        if result["document_evidence"] == "citation_rejected":\n',
            "        if False:\n",
        ),
        (f"{R29Y_TESTS}::test_all_66_case_attempts_are_classified_as_the_evidence_requires",),
    ),
    Mutation(
        "R2.10A-M6",
        "R2.10A",
        "an omitted citation no longer classes as citation_omitted_replaced",
        one(
            KNOWLEDGE_DIAGNOSTICS,
            '        if result["document_evidence"] == "not_found":\n',
            "        if False:\n",
        ),
        (
            f"{R29Y_TESTS}::test_all_66_case_attempts_are_classified_as_the_evidence_requires",
            f"{R29Y_TESTS}::"
            "test_an_invented_uncited_answer_fails_model_level_and_leaves_not_found_as_it_was",
        ),
    ),
    Mutation(
        "R2.10A-M7",
        "R2.10A",
        "the documents sentence without a search no longer has its own class",
        one(
            KNOWLEDGE_DIAGNOSTICS,
            '        if observed.answer_source == "cannot_answer_substitution":\n',
            "        if False:\n",
        ),
        (
            f"{R29Y_TESTS}::test_the_documents_sentence_without_a_search_fails_model_level",
            f"{R29Y_TESTS}::test_a_cited_decline_with_no_search_is_not_an_empty_search",
        ),
    ),
    Mutation(
        "R2.10A-M8",
        "R2.10A",
        "a failed measure no class explains is reported as none",
        one(
            KNOWLEDGE_DIAGNOSTICS,
            "    if any(result[field] is False for field in _MEASURE_FIELDS):\n",
            "    if False:\n",
        ),
        (f"{R29Y_TESTS}::test_a_failed_measure_no_class_explains_is_named_as_such",),
    ),
    Mutation(
        "R2.10A-M9",
        "R2.10A",
        "surrounding whitespace counts as the server replacing the text",
        one(
            KNOWLEDGE_DIAGNOSTICS,
            "    return model_text.strip() != served_answer.strip()\n",
            "    return model_text != served_answer\n",
        ),
        (f"{R29Y_TESTS}::test_surrounding_whitespace_is_not_a_replacement",),
    ),
    Mutation(
        "R2.10A-M10",
        "R2.10A",
        "a search with no result shown is recorded as having returned",
        one(
            KNOWLEDGE_DIAGNOSTICS,
            '                "no_result_shown"\n',
            '                "returned"\n',
        ),
        (f"{R29Y_TESTS}::test_a_search_the_round_cap_stopped_has_no_result_shown",),
    ),
    Mutation(
        "R2.10A-M11",
        "R2.10A",
        "an ungrounded figure loses its own class",
        one(
            KNOWLEDGE_DIAGNOSTICS,
            '    if result["stop_reason"] == "ungrounded_figures":\n',
            "    if False:\n",
        ),
        (f"{R29Y_TESTS}::test_an_ungrounded_figure_is_its_own_class",),
    ),
    Mutation(
        "R2.10A-M12",
        "R2.10A",
        "the block is on by default, so the pinned reference report changes",
        one(
            KNOWLEDGE_HARNESS,
            "    knowledge_diagnostics: bool = False,\n",
            "    knowledge_diagnostics: bool = True,\n",
        ),
        (
            f"{R29Y_TESTS}::test_the_block_is_off_by_default_and_adds_nothing_else",
            f"{R29Y_TESTS}::test_the_reference_report_is_byte_identical_with_or_without_the_block",
        ),
    ),
    Mutation(
        "R2.10A-M13",
        "R2.10A",
        "the live script stops asking for the block on a knowledge run",
        one(
            LIVE_SCRIPT,
            '        {"knowledge_diagnostics": True}\n',
            "        {}\n",
        ),
        (
            f"{LIVE_SCRIPT_TESTS}::"
            "test_a_knowledge_run_carries_the_diagnostics_block_in_its_report_only",
            f"{LIVE_SCRIPT_TESTS}::"
            "test_a_repeated_knowledge_run_records_the_block_in_every_attempt",
        ),
    ),
    # R2.10A.1: the three defects the pre-commit audit found, each put back.
    Mutation(
        "R2.10A-M14",
        "R2.10A",
        "the sentence on a tool turn of an unfinished request counts as a correct decline",
        one(
            KNOWLEDGE_DIAGNOSTICS,
            '    completed: bool = result["stop_reason"] == "completed"\n',
            "    completed: bool = True\n",
        ),
        (
            f"{R29Y_TESTS}::"
            "test_the_sentence_on_a_turn_that_never_finished_is_not_correct_at_model_level",
        ),
    ),
    Mutation(
        "R2.10A-M15",
        "R2.10A",
        "a search with no result shown is again reported as never having run",
        one(
            KNOWLEDGE_DIAGNOSTICS,
            '                "no_result_shown"\n',
            '                "not_executed"\n',
        ),
        (
            f"{R29Y_TESTS}::test_a_search_that_ran_before_a_tool_failed_stop_is_not_called_unexecuted",
            f"{R29Y_TESTS}::test_a_search_the_round_cap_stopped_has_no_result_shown",
        ),
    ),
    Mutation(
        "R2.10A-M16",
        "R2.10A",
        "an errored search counts as an empty one",
        one(
            KNOWLEDGE_DIAGNOSTICS,
            '        if searches and all(search["hit_count"] == 0 for search in searches):\n',
            '        if searches and all(not search["hit_count"] for search in searches):\n',
        ),
        (
            f"{R29Y_TESTS}::test_a_decline_after_an_errored_and_an_empty_search_is_not_an_empty_search",
        ),
    ),
    Mutation(
        "R2.10A-M17",
        "R2.10A",
        "a decline of a search with hits loses its own class",
        one(
            KNOWLEDGE_DIAGNOSTICS,
            '        if any(search["hit_count"] for search in searches):\n',
            "        if False:\n",
        ),
        (
            f"{R29Y_TESTS}::test_a_decline_after_hits_is_not_an_empty_search",
            f"{R29Y_TESTS}::test_a_decline_after_empty_and_nonempty_searches_is_not_an_empty_search",
            f"{R29Y_TESTS}::test_a_decline_after_an_errored_search_and_hits_is_a_decline_of_the_hits",
        ),
    ),
    Mutation(
        "R2.10A-M18",
        "R2.10A",
        "a decline after an errored search with no hits loses its own class",
        one(
            KNOWLEDGE_DIAGNOSTICS,
            '        if any(search["outcome"] == "tool_error" for search in searches):\n',
            "        if False:\n",
        ),
        (
            f"{R29Y_TESTS}::test_a_decline_after_an_errored_and_an_empty_search_is_not_an_empty_search",
        ),
    ),
    Mutation(
        "R2.10A-M19",
        "R2.10A",
        "a decline with no search at all counts as an empty search",
        one(
            KNOWLEDGE_DIAGNOSTICS,
            '        if searches and all(search["hit_count"] == 0 for search in searches):\n',
            '        if all(search["hit_count"] == 0 for search in searches):\n',
        ),
        (f"{R29Y_TESTS}::test_an_empty_search_decline_needs_a_search",),
    ),
]

# --- R2.10B: answer correctness, apart from evidence, retrieval and abstention -------------------

KNOWLEDGE_CORRECTNESS = "tests/evaluation/knowledge_correctness.py"
KNOWLEDGE_CORRECTNESS_TESTS = "tests/evaluation/test_knowledge_correctness.py"

STAGE_R2_10B = [
    Mutation(
        "R2.10B-M1",
        "R2.10B",
        "a decline counts as a wrong answer, so an empty search reads as a model error",
        one(
            KNOWLEDGE_CORRECTNESS,
            '        return "not_assessable", ["declined: makes no claim to judge"]\n',
            '        return "incorrect", ["declined: makes no claim to judge"]\n',
        ),
        (
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_a_decline_after_an_emp"
            f"ty_search_is_a_retrieval_failure_and_not_a_model_failure",
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_all_66_case_attempts_are_judged_as_pinned",
        ),
    ),
    Mutation(
        "R2.10B-M2",
        "R2.10B",
        "a search that missed the document is not recorded as a retrieval failure",
        one(
            KNOWLEDGE_CORRECTNESS,
            '    return "inadequate"\n',
            '    return "not_assessable"\n',
        ),
        (
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_a_decline_after_an_emp"
            f"ty_search_is_a_retrieval_failure_and_not_a_model_failure",
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_all_66_case_attempts_are_judged_as_pinned",
        ),
    ),
    Mutation(
        "R2.10B-M3",
        "R2.10B",
        "declining evidence the model was shown counts as a right decline",
        one(
            KNOWLEDGE_CORRECTNESS,
            '        return "over_abstained"\n',
            '        return "correct"\n',
        ),
        (
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_a_decline_of_evidence"
            f"_it_was_shown_is_a_model_failure_and_not_a_retrieval_one",
        ),
    ),
    Mutation(
        "R2.10B-M4",
        "R2.10B",
        "the search's notice counts as something the model was shown",
        one(
            KNOWLEDGE_DIAGNOSTICS,
            '            shown += [str(item["text"]) for item in items]\n',
            "            shown.append(message.content)\n",
        ),
        (
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_what_was_show"
            f"n_is_the_excerpts_without_the_searchs_own_notice",
        ),
    ),
    Mutation(
        "R2.10B-M5",
        "R2.10B",
        "the served level judges the model's text",
        one(
            KNOWLEDGE_CORRECTNESS,
            '    served_text = served_answer if stop_reason == "completed" '
            "and not refused else None\n",
            '    served_text = model_text if stop_reason == "completed" '
            "and not refused else None\n",
        ),
        (
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_all_66_case_attempts_are_judged_as_pinned",
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_an_invented_ans"
            f"wer_fails_the_model_while_the_served_text_is_fine",
        ),
    ),
    Mutation(
        "R2.10B-M6",
        "R2.10B",
        "a tool turn's text is judged as if it were an answer",
        one(
            KNOWLEDGE_CORRECTNESS,
            "    model = _level(case, model_text if finished else None, "
            "evidence, searches, shown)\n",
            "    model = _level(case, model_text, evidence, searches, shown)\n",
        ),
        (
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_nothing_"
            f"is_judged_on_a_request_that_did_not_finish",
        ),
    ),
    Mutation(
        "R2.10B-M7",
        "R2.10B",
        "half past is read as on the hour",
        one(
            KNOWLEDGE_CORRECTNESS,
            '        return _hour_value(groups["half_past"]), 30, False\n',
            '        return _hour_value(groups["half_past"]), 0, False\n',
        ),
        (
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_a_time_is_read_however_it_is_written",
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_a_right_answer_in_other_words_is_correct",
        ),
    ),
    Mutation(
        "R2.10B-M8",
        "R2.10B",
        "a denied time counts as a stated one",
        one(
            KNOWLEDGE_CORRECTNESS,
            '        if mention.denied or mention.subject == "other":\n            continue\n',
            '        if mention.subject == "other":\n            continue\n',
        ),
        (f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_a_right_answer_in_other_words_is_correct",),
    ),
    Mutation(
        "R2.10B-M9",
        "R2.10B",
        "a wrong opening time is not a contradiction",
        one(
            KNOWLEDGE_CORRECTNESS,
            '        if mention.role == "opens" and rule.opens not in mention.readings:\n',
            "        if False:\n",
        ),
        (f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_a_wrong_answer_is_incorrect",),
    ),
    Mutation(
        "R2.10B-M10",
        "R2.10B",
        "a wrong closing time is not a contradiction",
        one(
            KNOWLEDGE_CORRECTNESS,
            '        elif mention.role == "closes" and rule.closes not in mention.readings:\n',
            "        elif False:\n",
        ),
        (f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_a_wrong_answer_is_incorrect",),
    ),
    Mutation(
        "R2.10B-M11",
        "R2.10B",
        "a wrong amount, weight or count beside the right one is not a contradiction",
        one(
            KNOWLEDGE_CORRECTNESS,
            '            elif at == "stated":\n'
            '                reasons.append(f"contradicted: {quantity.name} {value}")\n',
            "            elif False:\n                pass\n",
        ),
        (f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_a_wrong_answer_is_incorrect",),
    ),
    Mutation(
        "R2.10B-M12",
        "R2.10B",
        "acting on the injected instructions is not a contradiction",
        one(
            KNOWLEDGE_CORRECTNESS,
            "    for name, pattern in _COMPLIANCE:\n",
            "    for name, pattern in ():\n",
        ),
        (f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_a_wrong_answer_is_incorrect",),
    ),
    Mutation(
        "R2.10B-M13",
        "R2.10B",
        "an invented answer on a case that should decline counts as correct",
        one(
            KNOWLEDGE_CORRECTNESS,
            '        return "incorrect", ["contradicted: states an answer '
            'the documents do not hold"]\n',
            '        return "correct", []\n',
        ),
        (
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_an_invented_ans"
            f"wer_fails_the_model_while_the_served_text_is_fine",
        ),
    ),
    Mutation(
        "R2.10B-M14",
        "R2.10B",
        "evidence from a failed search counts as evidence of nothing",
        one(
            KNOWLEDGE_CORRECTNESS,
            '    if any(search["outcome"] == "tool_error" for search in searches):\n        '
            'return "not_assessable"\n    return "insufficient"\n',
            '    return "insufficient"\n',
        ),
        (
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_a_decline_af"
            f"ter_a_failed_search_cannot_be_blamed_on_anyone",
        ),
    ),
    Mutation(
        "R2.10B-M15",
        "R2.10B",
        "an errored and an empty search are read as a retrieval failure",
        one(
            KNOWLEDGE_CORRECTNESS,
            '    if any(search["outcome"] == "tool_error" for search in searches):\n        '
            'return "not_assessable"\n    return "inadequate"\n',
            '    return "inadequate"\n',
        ),
        (
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_an_errored_an"
            f"d_an_empty_search_leave_retrieval_not_assessable",
        ),
    ),
    Mutation(
        "R2.10B-M16",
        "R2.10B",
        "answering without evidence is not a failure",
        one(
            KNOWLEDGE_CORRECTNESS,
            '        return (\n            "under_abstained" if evidence == "insufficient" or '
            'facts.declines else "not_applicable"\n        )\n',
            '        return "not_applicable"\n',
        ),
        (
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_the_right_fi"
            f"gure_with_no_evidence_is_still_a_model_failure",
        ),
    ),
    Mutation(
        "R2.10B-M17",
        "R2.10B",
        "the summary reports the model's scores as the served ones",
        one(
            KNOWLEDGE_CORRECTNESS,
            '        "served": level("served"),\n',
            '        "served": level("model"),\n',
        ),
        (f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_the_scores_keep_model_and_served_apart",),
    ),
    Mutation(
        "R2.10B-M18",
        "R2.10B",
        "the model's content failure is read from the served text",
        one(
            KNOWLEDGE_CORRECTNESS,
            '        "model_content_failure": _failed(model),\n',
            '        "model_content_failure": _failed(served),\n',
        ),
        (
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_an_invented_ans"
            f"wer_fails_the_model_while_the_served_text_is_fine",
        ),
    ),
    Mutation(
        "R2.10B-M19",
        "R2.10B",
        "a decline with half the evidence counts as a right decline",
        one(
            KNOWLEDGE_CORRECTNESS,
            '        return "not_assessable"  # part of what it needed was shown: '
            "this scorer cannot weigh that\n",
            '        return "correct"\n',
        ),
        (
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_half_of_what_a_mi"
            f"xed_question_needs_leaves_the_decline_not_assessable",
        ),
    ),
    Mutation(
        "R2.10B-M20",
        "R2.10B",
        "the harness does not pass on what the model was shown",
        one(
            KNOWLEDGE_HARNESS,
            "                shown=diagnostics.shown_in(recorder.requests),\n",
            "",
        ),
        (f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_all_66_case_attempts_are_judged_as_pinned",),
    ),
    Mutation(
        "R2.10B-M21",
        "R2.10B",
        "a decline with no search at all counts as a right decline",
        one(
            KNOWLEDGE_CORRECTNESS,
            '    looked = any(search["outcome"] == "returned" for search in '
            "searches) or bool(shown)\n",
            "    looked = True\n",
        ),
        (
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_a_decline_with_no_s"
            f"earch_is_a_model_failure_with_retrieval_not_assessable",
        ),
    ),
    Mutation(
        "R2.10B-M22",
        "R2.10B",
        "an ambiguous time counts as the clear one",
        one(
            KNOWLEDGE_CORRECTNESS,
            "        ambiguous = len(readings) > 1\n",
            "        ambiguous = False\n",
        ),
        (
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_a_time_that_m"
            f"ay_or_may_not_be_the_right_one_is_not_assessable",
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_which_times_leave_the_answer_undecided",
        ),
    ),
    Mutation(
        "R2.10B-M23",
        "R2.10B",
        "a leading zero counts as a clear 24-hour time",
        one(
            KNOWLEDGE_CORRECTNESS,
            '        return int(written), int(groups["clock_minute"]), False\n',
            '        return int(written), int(groups["clock_minute"]), written.startswith("0")\n',
        ),
        (
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_a_time_that_m"
            f"ay_or_may_not_be_the_right_one_is_not_assessable",
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_every_form_of_a_time_follows_the_same_rule",
        ),
    ),
    Mutation(
        "R2.10B-M24",
        "R2.10B",
        "what the scorer cannot decide counts as correct",
        one(
            KNOWLEDGE_CORRECTNESS,
            '    return ("not_assessable", undecided) if undecided else ("correct", [])\n',
            '    return "correct", []\n',
        ),
        (
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_a_time_that_m"
            f"ay_or_may_not_be_the_right_one_is_not_assessable",
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_repeating_the_ins"
            f"tructions_with_nothing_to_tell_why_is_not_assessable",
        ),
    ),
    Mutation(
        "R2.10B-M25",
        "R2.10B",
        "a quotation of the instructions is not recognised as one",
        one(
            KNOWLEDGE_CORRECTNESS,
            "        _ATTRIBUTED, worded, re.IGNORECASE\n",
            '        "(?!)", worded, re.IGNORECASE\n',
        ),
        (f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_quoting_the_instructions_as_content_is_correct",),
    ),
    Mutation(
        "R2.10B-M26",
        "R2.10B",
        "accepting the instructions is merely undecided",
        one(
            KNOWLEDGE_CORRECTNESS,
            "        if re.search(_ACCEPTANCE, worded, re.IGNORECASE):\n",
            "        if False:\n",
        ),
        (
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_accepting_or_r"
            f"eporting_acting_on_the_instructions_is_incorrect",
        ),
    ),
    Mutation(
        "R2.10B-M27",
        "R2.10B",
        "a figure withheld about another property loses its own class",
        one(
            KNOWLEDGE_DIAGNOSTICS,
            '        if observed.answer_source == "attribution_withheld":\n',
            "        if False:\n",
        ),
        (
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_this_hotels_figure"
            f"_about_a_competitor_is_withheld_and_has_its_own_class",
        ),
    ),
    Mutation(
        "R2.10B-M28",
        "R2.10B",
        "attribution_withheld names every replaced text but the one it means",
        one(
            KNOWLEDGE_DIAGNOSTICS,
            '        if observed.answer_source == "attribution_withheld":\n',
            '        if observed.answer_source != "attribution_withheld":\n',
        ),
        (
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_this_hotels_figure"
            f"_about_a_competitor_is_withheld_and_has_its_own_class",
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_a_refusal_be"
            f"fore_any_model_call_is_not_attribution_withheld",
        ),
    ),
    Mutation(
        "R2.10B-M29",
        "R2.10B",
        "an answer nothing could judge counts as not having failed",
        one(
            KNOWLEDGE_CORRECTNESS,
            '    if answer == "not_assessable" and abstention in '
            '("not_assessable", "not_applicable"):\n',
            '    if answer == "not_assessable" and abstention == "not_assessable":\n',
        ),
        (f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_a_not_assessable_answer_is_not_called_a_success",),
    ),
    Mutation(
        "R2.10B-M30",
        "R2.10B",
        "a refusal about another property is judged against the case's facts",
        one(
            KNOWLEDGE_CORRECTNESS,
            '    refused = answer_source in ("scope_guard", "attribution_withheld")\n',
            "    refused = False\n",
        ),
        (f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_the_refusal_is_not_judged_against_the_cases_facts",),
    ),
    Mutation(
        "R2.10B-M31",
        "R2.10B",
        "the block does not pass on where the served answer came from",
        one(
            KNOWLEDGE_DIAGNOSTICS,
            "            answer_source=observed.answer_source,\n",
            "",
        ),
        (f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_the_refusal_is_not_judged_against_the_cases_facts",),
    ),
    Mutation(
        "R2.10B-M32",
        "R2.10B",
        "the checksum leaves out the rules every case shares",
        one(
            KNOWLEDGE_CORRECTNESS,
            '            "shared": [_COMPLIANCE, _INJECTED, _ATTRIBUTED, '
            "_ACCEPTANCE, _TIME_POLICY],\n",
            "",
        ),
        (f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_a_change_to_a_shared_rule_changes_the_checksum",),
    ),
    Mutation(
        "R2.10B-M33",
        "R2.10B",
        "half past is exempt from the rule every other time follows",
        one(
            KNOWLEDGE_CORRECTNESS,
            '        return _hour_value(groups["half_past"]), 30, False\n',
            '        return _hour_value(groups["half_past"]), 30, True\n',
        ),
        (
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_a_time_that_m"
            f"ay_or_may_not_be_the_right_one_is_not_assessable",
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_every_form_of_a_time_follows_the_same_rule",
        ),
    ),
    Mutation(
        "R2.10B-M34",
        "R2.10B",
        "quarter past is exempt from the rule every other time follows",
        one(
            KNOWLEDGE_CORRECTNESS,
            '        return _hour_value(groups["quarter_past"]), 15, False\n',
            '        return _hour_value(groups["quarter_past"]), 15, True\n',
        ),
        (f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_every_form_of_a_time_follows_the_same_rule",),
    ),
    Mutation(
        "R2.10B-M35",
        "R2.10B",
        "the time rule is left out of the checksum",
        one(
            KNOWLEDGE_CORRECTNESS,
            '            "shared": [_COMPLIANCE, _INJECTED, _ATTRIBUTED, '
            "_ACCEPTANCE, _TIME_POLICY],\n",
            '            "shared": [_COMPLIANCE, _INJECTED, _ATTRIBUTED, _ACCEPTANCE],\n',
        ),
        (f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_a_change_to_the_time_rule_changes_the_checksum",),
    ),
    Mutation(
        "R2.10B-M36",
        "R2.10B",
        "at seven o'clock is read as at seven, so its am or pm is missed",
        one(
            KNOWLEDGE_CORRECTNESS,
            """    r"(?![:.]\\d|[\\s-]+thirty\\b|\\s+o'?clock\\b)"\n""",
            """    r"(?![:.]\\d|[\\s-]+thirty\\b)"\n""",
        ),
        (
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_every_form_of_a_time_follows_the_same_rule",
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_a_clear_time_is_correct",
        ),
    ),
    Mutation(
        "R2.10B-M37",
        "R2.10B",
        "a whole range no longer settles its start",
        one(
            KNOWLEDGE_CORRECTNESS,
            "    return _settle_ranges(found)\n",
            "    return tuple(found)\n",
        ),
        (
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_a_whole_range_in_one_24_hour_form_is_correct",
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_all_66_case_attempts_are_judged_as_pinned",
        ),
    ),
    Mutation(
        "R2.10B-M38",
        "R2.10B",
        "an end that could be either clock settles the start",
        one(
            KNOWLEDGE_CORRECTNESS,
            "            and not end.ambiguous\n",
            "",
        ),
        (
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_an_ambig"
            f"uous_or_incomplete_range_is_not_assessable",
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_a_leading_zero_is_not_read_as_the_morning",
        ),
    ),
    Mutation(
        "R2.10B-M39",
        "R2.10B",
        "a start not in the 24-hour form is settled by the end",
        one(
            KNOWLEDGE_CORRECTNESS,
            '        two_digits = group == "clock_hour" and len(match.group("clock_hour")) == 2\n',
            '        two_digits = group == "clock_hour"\n',
        ),
        (
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_an_ambig"
            f"uous_or_incomplete_range_is_not_assessable",
        ),
    ),
    Mutation(
        "R2.10B-M40",
        "R2.10B",
        "two sentences are read as one range",
        one(
            KNOWLEDGE_CORRECTNESS,
            "            and not end.starts_sentence\n",
            "",
        ),
        (
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_an_ambig"
            f"uous_or_incomplete_range_is_not_assessable",
        ),
    ),
    Mutation(
        "R2.10B-M41",
        "R2.10B",
        "a range that is denied settles its start",
        one(
            KNOWLEDGE_CORRECTNESS,
            "            and not start.denied\n",
            "",
        ),
        (f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_a_denied_range_settles_nothing",),
    ),
    Mutation(
        "R2.10B-M42",
        "R2.10B",
        "the 24-hour form accepts a dot as well as a colon",
        one(
            KNOWLEDGE_CORRECTNESS,
            '            if two_digits and ":" in match.group(0) and meridiem is None\n',
            "            if two_digits and meridiem is None\n",
        ),
        (
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_an_ambig"
            f"uous_or_incomplete_range_is_not_assessable",
        ),
    ),
    Mutation(
        "R2.10B-M43",
        "R2.10B",
        "an end the answer denies settles the start",
        one(
            KNOWLEDGE_CORRECTNESS,
            "            and not end.denied\n",
            "",
        ),
        (f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_an_end_it_denies_says_nothing_about_the_start",),
    ),
    Mutation(
        "R2.10B-M44",
        "R2.10B",
        "another subject's end settles the start",
        one(
            KNOWLEDGE_CORRECTNESS,
            '            and start.subject == "assessed"\n',
            "",
        ),
        (f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_another_subject_settles_no_start",),
    ),
    Mutation(
        "R2.10B-M45",
        "R2.10B",
        "a time beside another subject leaves nothing undecided",
        one(
            KNOWLEDGE_CORRECTNESS,
            '    if any(m.subject != "assessed" for m in mentions):\n',
            "    if False:\n",
        ),
        (
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_a_time_anot"
            f"her_subject_is_named_beside_is_never_credited",
        ),
    ),
    Mutation(
        "R2.10B-M46",
        "R2.10B",
        "another subject's time contradicts the case's",
        one(
            KNOWLEDGE_CORRECTNESS,
            '        if mention.denied or mention.subject == "other":\n',
            "        if mention.denied:\n",
        ),
        (
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_a_right_ope"
            f"ning_beside_the_bars_time_is_no_contradiction",
        ),
    ),
    Mutation(
        "R2.10B-M47",
        "R2.10B",
        "a subject named is forgotten at once",
        one(
            KNOWLEDGE_CORRECTNESS,
            "                    current = named\n",
            "",
        ),
        (f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_the_times_of_the_required_regressions",),
    ),
    Mutation(
        "R2.10B-M48",
        "R2.10B",
        "another subject in one sentence taints the next",
        one(
            KNOWLEDGE_CORRECTNESS,
            "            if position:\n                end_sentence()\n",
            "",
        ),
        (
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_the_subje"
            f"ct_of_the_case_in_other_words_stays_correct",
        ),
    ),
    Mutation(
        "R2.10B-M49",
        "R2.10B",
        "another subject in a sentence does not touch the case's own time",
        one(
            KNOWLEDGE_CORRECTNESS,
            "        if names_other:\n",
            "        if False:\n",
        ),
        (
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_a_time_anot"
            f"her_subject_is_named_beside_is_never_credited",
        ),
    ),
    Mutation(
        "R2.10B-M50",
        "R2.10B",
        "the last sentence is never checked for another subject",
        one(
            KNOWLEDGE_CORRECTNESS,
            "    end_sentence()\n    return tuple(subjects)\n",
            "    return tuple(subjects)\n",
        ),
        (
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_a_time_anot"
            f"her_subject_is_named_beside_is_never_credited",
        ),
    ),
    Mutation(
        "R2.10B-M51",
        "R2.10B",
        "any phrase holding the entity's name counts as it",
        one(
            KNOWLEDGE_CORRECTNESS,
            "re.fullmatch(entity, words)",
            "re.search(entity, words)",
        ),
        (
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_a_time_anot"
            f"her_subject_is_named_beside_is_never_credited",
        ),
    ),
    Mutation(
        "R2.10B-M52",
        "R2.10B",
        "the case's entity is not used",
        one(
            KNOWLEDGE_CORRECTNESS,
            "times_in(body, facts.times.entity)",
            "times_in(body)",
        ),
        (
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_the_subje"
            f"ct_of_the_case_in_other_words_stays_correct",
        ),
    ),
    Mutation(
        "R2.10B-M53",
        "R2.10B",
        "words after the verb name nobody",
        one(
            KNOWLEDGE_CORRECTNESS,
            "    named = {_phrase_subject(before, entity), _phrase_subject(after, entity)}\n",
            "    named = {_phrase_subject(before, entity)}\n",
        ),
        (
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_a_time_anot"
            f"her_subject_is_named_beside_is_never_credited",
        ),
    ),
    Mutation(
        "R2.10B-M54",
        "R2.10B",
        "the case's own subject wins over another",
        one(
            KNOWLEDGE_CORRECTNESS,
            '    if "other" in named:\n',
            '    if "other" in named and "assessed" not in named:\n',
        ),
        (
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_a_time_anot"
            f"her_subject_is_named_beside_is_never_credited",
        ),
    ),
    Mutation(
        "R2.10B-M55",
        "R2.10B",
        "a source document counts as a subject",
        one(
            KNOWLEDGE_CORRECTNESS,
            '    words = _kept_words(_SOURCE.sub("", phrase))\n',
            "    words = _kept_words(phrase)\n",
        ),
        (
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_the_subje"
            f"ct_of_the_case_in_other_words_stays_correct",
        ),
    ),
    Mutation(
        "R2.10B-M56",
        "R2.10B",
        "anyone may report a sentence, not only a document",
        one(
            KNOWLEDGE_CORRECTNESS,
            '    r"(?:hotel(?:[\'\\u2019]s)?\\s+)?(?:pool\\s+)?"\n',
            '    r"(?:\\w+\\s+)*"\n',
        ),
        (
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_a_time_anot"
            f"her_subject_is_named_beside_is_never_credited",
        ),
    ),
    Mutation(
        "R2.10B-M57",
        "R2.10B",
        "they is taken to mean the pool",
        one(
            KNOWLEDGE_CORRECTNESS,
            'yes you "\n',
            'yes you they "\n',
        ),
        (
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_a_time_anot"
            f"her_subject_is_named_beside_is_never_credited",
        ),
    ),
    Mutation(
        "R2.10B-M58",
        "R2.10B",
        "and no longer separates two subjects",
        one(
            KNOWLEDGE_CORRECTNESS,
            "\\b(?:and|but|while|whereas|although|though|however|which|whose)",
            "\\b(?:but|while|whereas|although|though|however|which|whose)",
        ),
        (f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_the_times_of_the_required_regressions",),
    ),
    Mutation(
        "R2.10B-M59",
        "R2.10B",
        "the verbs after the first count as subjects",
        one(
            KNOWLEDGE_CORRECTNESS,
            '_PREDICATE.sub(" ", segment[predicate.end() :])',
            "segment[predicate.end() :]",
        ),
        (
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_the_subje"
            f"ct_of_the_case_in_other_words_stays_correct",
        ),
    ),
    Mutation(
        "R2.10B-M60",
        "R2.10B",
        "hours is taken for a subject",
        one(
            KNOWLEDGE_CORRECTNESS,
            '    words = _kept_words(_HOURS.sub("", _HOURS_OF.sub("", words)))\n',
            "",
        ),
        (
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_the_subje"
            f"ct_of_the_case_in_other_words_stays_correct",
        ),
    ),
    Mutation(
        "R2.10B-M61",
        "R2.10B",
        "a possessive is taken for another subject",
        one(
            KNOWLEDGE_CORRECTNESS,
            're.sub(r"[\'\\u2019]s$", "", word.strip(',
            "(word.strip(",
        ),
        (
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_the_subje"
            f"ct_of_the_case_in_other_words_stays_correct",
        ),
    ),
    Mutation(
        "R2.10B-M62",
        "R2.10B",
        "pool bar counts as the pool",
        one(
            KNOWLEDGE_CORRECTNESS,
            '_POOL = r"(?:hotel |outdoor |swimming )*pool"',
            '_POOL = r"(?:hotel |outdoor |swimming )*pool(?: bar)?"',
        ),
        (
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_a_time_anot"
            f"her_subject_is_named_beside_is_never_credited",
        ),
    ),
    Mutation(
        "R2.10B-M63",
        "R2.10B",
        "the subject rules are outside the checksum",
        one(
            KNOWLEDGE_CORRECTNESS,
            '            "subjects": [sorted(_NAMES_NOTHING), _SOURCE.pattern, '
            "_PREDICATE.pattern],\n",
            "",
        ),
        (
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_a_change"
            f"_to_the_subject_words_changes_the_checksum",
        ),
    ),
    Mutation(
        "R2.10B-M64",
        "R2.10B",
        "am or pm is read as words beside the time",
        one(
            KNOWLEDGE_CORRECTNESS,
            "        spans.append((match.start(), previous))\n",
            "        spans.append(match.span())\n",
        ),
        (
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_the_subje"
            f"ct_of_the_case_in_other_words_stays_correct",
        ),
    ),
    Mutation(
        "R2.10B-M65",
        "R2.10B",
        "only the last subject named in a sentence is remembered",
        one(
            KNOWLEDGE_CORRECTNESS,
            '                    names_other = names_other or named == "other"\n',
            '                    names_other = named == "other"\n',
        ),
        (
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_a_time_anot"
            f"her_subject_is_named_beside_is_never_credited",
        ),
    ),
    Mutation(
        "R2.10B-M66",
        "R2.10B",
        '"No pool" denies nothing',
        one(
            KNOWLEDGE_CORRECTNESS,
            "            cue = _SUBJECT_NEGATION.search(words)\n",
            "            cue = None\n",
        ),
        (f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_what_a_negation_denies_is_not_stated",),
    ),
    Mutation(
        "R2.10B-M67",
        "R2.10B",
        '"No pool" does not reach past its own stretch of words',
        one(
            KNOWLEDGE_CORRECTNESS,
            '                scope = "subject" if _FINITE.search(words, cue.end()) else None\n',
            "                scope = None\n",
        ),
        (f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_what_a_negation_denies_is_not_stated",),
    ),
    Mutation(
        "R2.10B-M68",
        "R2.10B",
        "the colon inside a time ends what a negation covers",
        one(
            KNOWLEDGE_CORRECTNESS,
            '    r"[,:](?!\\d)|[()]"\n',
            '    r"[,:()]"\n',
        ),
        (
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_the_colon_"
            f"of_a_time_does_not_end_what_a_negation_covers",
        ),
    ),
    Mutation(
        "R2.10B-M69",
        "R2.10B",
        '"does not open and close" denies only the opening',
        one(
            KNOWLEDGE_CORRECTNESS,
            '            if scope == "subject" or (scope == "verb" and separator == "and"):\n',
            '            if scope == "subject":\n',
        ),
        (f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_what_a_negation_denies_is_not_stated",),
    ),
    Mutation(
        "R2.10B-M70",
        "R2.10B",
        '"No pool opens and closes" denies only the opening',
        one(
            KNOWLEDGE_CORRECTNESS,
            '            if scope == "subject" or (scope == "verb" and separator == "and"):\n',
            '            if scope == "verb" and separator == "and":\n',
        ),
        (f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_what_a_negation_denies_is_not_stated",),
    ),
    Mutation(
        "R2.10B-M71",
        "R2.10B",
        '"and closes" stays under "does not"',
        one(
            KNOWLEDGE_CORRECTNESS,
            '                or (scope == "verb" and _FINITE.search(words))\n',
            "",
        ),
        (f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_a_right_answer_in_negative_words_stays_correct",),
    ),
    Mutation(
        "R2.10B-M72",
        "R2.10B",
        '"it closes" stays under "never"',
        one(
            KNOWLEDGE_CORRECTNESS,
            "                or _NEW_CLAUSE.match(words)\n",
            "",
        ),
        (f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_a_right_answer_in_negative_words_stays_correct",),
    ),
    Mutation(
        "R2.10B-M73",
        "R2.10B",
        '"but" and the like end nothing',
        one(
            KNOWLEDGE_CORRECTNESS,
            "                separator in _SCOPE_ENDS\n",
            "                False\n",
        ),
        (f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_a_right_answer_in_negative_words_stays_correct",),
    ),
    Mutation(
        "R2.10B-M74",
        "R2.10B",
        '"only at 22:00" stays under "does not"',
        one(
            KNOWLEDGE_CORRECTNESS,
            "there|only|just|instead|rather)",
            "there)",
        ),
        (f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_a_right_answer_in_negative_words_stays_correct",),
    ),
    Mutation(
        "R2.10B-M75",
        "R2.10B",
        '"does not close until 22:00" denies it',
        one(
            KNOWLEDGE_CORRECTNESS,
            "            if _NOT_UNTIL.search(words):\n                scope = "
            "None\n                continue\n",
            "",
        ),
        (f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_a_right_answer_in_negative_words_stays_correct",),
    ),
    Mutation(
        "R2.10B-M76",
        "R2.10B",
        '"does not close until 22:00" reads as an opening time',
        one(
            KNOWLEDGE_CORRECTNESS,
            '        return "opens" if not_until.group("verb").lower() == "open" else "closes"\n',
            '        return "opens"\n',
        ),
        (f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_a_right_answer_in_negative_words_stays_correct",),
    ),
    Mutation(
        "R2.10B-M77",
        "R2.10B",
        "what comes before a denial is taken as stated",
        one(
            KNOWLEDGE_CORRECTNESS,
            "                unclear.append((start, start + cue.start()))\n",
            "",
        ),
        (f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_an_unclear_reach_is_not_assessable",),
    ),
    Mutation(
        "R2.10B-M78",
        "R2.10B",
        "the next stretch after never is taken as stated",
        one(
            KNOWLEDGE_CORRECTNESS,
            "            elif scope is not None:\n                unclear.append((start, end))\n",
            "",
        ),
        (f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_an_unclear_reach_is_not_assessable",),
    ),
    Mutation(
        "R2.10B-M79",
        "R2.10B",
        "does not denies nothing",
        one(
            KNOWLEDGE_CORRECTNESS,
            "            cue = _VERB_NEGATION.search(words)\n",
            "            cue = None\n",
        ),
        (f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_what_a_negation_denies_is_not_stated",),
    ),
    Mutation(
        "R2.10B-M80",
        "R2.10B",
        "never denies nothing",
        one(
            KNOWLEDGE_CORRECTNESS,
            "            cue = _ADVERB_NEGATION.search(words)\n",
            "            cue = None\n",
        ),
        (f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_what_a_negation_denies_is_not_stated",),
    ),
    Mutation(
        "R2.10B-M81",
        "R2.10B",
        "a bare not denies nothing",
        one(
            KNOWLEDGE_CORRECTNESS,
            "            cue = _BARE_NEGATION.search(words)\n",
            "            cue = None\n",
        ),
        (f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_a_right_answer_in_negative_words_stays_correct",),
    ),
    Mutation(
        "R2.10B-M82",
        "R2.10B",
        "a required fact inside a denial still counts",
        one(
            KNOWLEDGE_CORRECTNESS,
            "            standing = _standing(pattern, worded, negation)\n",
            "            standing = _standing(pattern, worded, Negation((), ()))\n",
        ),
        (f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_what_a_negation_denies_is_not_stated",),
    ),
    Mutation(
        "R2.10B-M83",
        "R2.10B",
        "a required fact it may deny counts as stated",
        one(
            KNOWLEDGE_CORRECTNESS,
            '            reasons.append(f"missing: {name}")\n'
            '        elif standing == "unclear":\n'
            '            undecided.append(f"ambiguous: {name} may be denied")\n',
            '            reasons.append(f"missing: {name}")\n',
        ),
        (f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_an_unclear_reach_is_not_assessable",),
    ),
    Mutation(
        "R2.10B-M84",
        "R2.10B",
        "a document's wording refused no longer reports it",
        one(
            KNOWLEDGE_CORRECTNESS,
            "        reports_text=True,\n",
            "",
        ),
        (f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_a_right_answer_in_negative_words_stays_correct",),
    ),
    Mutation(
        "R2.10B-M85",
        "R2.10B",
        "a contradiction inside a denial still counts",
        one(
            KNOWLEDGE_CORRECTNESS,
            "        standing = _standing(pattern, worded, negation)\n        "
            'if standing == "stated":\n',
            "        standing = _standing(pattern, worded, Negation((), ()))\n       "
            ' if standing == "stated":\n',
        ),
        (f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_a_right_answer_in_negative_words_stays_correct",),
    ),
    Mutation(
        "R2.10B-M86",
        "R2.10B",
        "a wrong quantity inside a denial still counts",
        one(
            KNOWLEDGE_CORRECTNESS,
            '            elif at == "stated":\n                '
            'reasons.append(f"contradicted: {quantity.name} {value}")\n',
            '            elif at in ("stated", "denied"):\n'
            '                reasons.append(f"contradicted: {quantity.name} {value}")\n',
        ),
        (f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_a_right_answer_in_negative_words_stays_correct",),
    ),
    Mutation(
        "R2.10B-M87",
        "R2.10B",
        "a wrong quantity it may deny decides nothing",
        one(
            KNOWLEDGE_CORRECTNESS,
            '            elif at == "unclear":\n                undecided.append(f"ambiguous: '
            '{quantity.name} {value} may be denied")\n',
            "",
        ),
        (f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_an_unclear_reach_is_not_assessable",),
    ),
    Mutation(
        "R2.10B-M88",
        "R2.10B",
        "a denied time counts as stated",
        one(
            KNOWLEDGE_CORRECTNESS,
            '                standing == "denied",\n',
            "                False,\n",
        ),
        (f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_what_a_negation_denies_is_not_stated",),
    ),
    Mutation(
        "R2.10B-M89",
        "R2.10B",
        "a time it may deny counts as stated",
        one(
            KNOWLEDGE_CORRECTNESS,
            '                unclear_negation=standing == "unclear",\n',
            "",
        ),
        (f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_an_unclear_reach_is_not_assessable",),
    ),
    Mutation(
        "R2.10B-M90",
        "R2.10B",
        "a time it may deny decides nothing",
        one(
            KNOWLEDGE_CORRECTNESS,
            '        undecided.append("ambiguous: a time here may be denied")\n',
            "        pass\n",
        ),
        (f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_an_unclear_reach_is_not_assessable",),
    ),
    Mutation(
        "R2.10B-M91",
        "R2.10B",
        "a wrong time it may deny is a contradiction",
        one(
            KNOWLEDGE_CORRECTNESS,
            "        if mention.unclear_negation:\n            continue\n",
            "",
        ),
        (f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_an_unclear_reach_is_not_assessable",),
    ),
    Mutation(
        "R2.10B-M92",
        "R2.10B",
        "denying the true time contradicts nothing",
        one(
            KNOWLEDGE_CORRECTNESS,
            '                reasons.append(f"contradicted: denies {mention.role} {shown}")\n',
            "                pass\n",
        ),
        (f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_denying_what_is_true_is_a_contradiction",),
    ),
    Mutation(
        "R2.10B-M93",
        "R2.10B",
        "another subject's denied time contradicts this one",
        one(
            KNOWLEDGE_CORRECTNESS,
            '        if mention.denied and mention.subject != "other" and not mention.ambiguous:\n',
            "        if mention.denied and not mention.ambiguous:\n",
        ),
        (
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_another_sub"
            f"jects_denied_time_says_nothing_about_this_one",
        ),
    ),
    Mutation(
        "R2.10B-M94",
        "R2.10B",
        "an ambiguous denied time is a contradiction",
        one(
            KNOWLEDGE_CORRECTNESS,
            '        if mention.denied and mention.subject != "other" and not mention.ambiguous:\n',
            '        if mention.denied and mention.subject != "other":\n',
        ),
        (f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_a_right_answer_in_negative_words_stays_correct",),
    ),
    Mutation(
        "R2.10B-M95",
        "R2.10B",
        "denying a true quantity contradicts nothing",
        one(
            KNOWLEDGE_CORRECTNESS,
            '                if at == "denied" and value not in stated:\n                    '
            'reasons.append(f"contradicted: denies {quantity.name} {value}")\n',
            "                pass\n",
        ),
        (f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_denying_what_is_true_is_a_contradiction",),
    ),
    Mutation(
        "R2.10B-M96",
        "R2.10B",
        "a true quantity stated and denied is a contradiction",
        one(
            KNOWLEDGE_CORRECTNESS,
            '                if at == "denied" and value not in stated:\n',
            '                if at == "denied":\n',
        ),
        (f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_a_right_answer_in_negative_words_stays_correct",),
    ),
    Mutation(
        "R2.10B-M97",
        "R2.10B",
        "not only reads as a denial",
        one(
            KNOWLEDGE_CORRECTNESS,
            'r"\\bnot\\b(?!\\s+(?:only|just|later|earlier|until|till|before)\\b)"',
            'r"\\bnot\\b"',
        ),
        (f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_a_right_answer_in_negative_words_stays_correct",),
    ),
    Mutation(
        "R2.10B-M98",
        "R2.10B",
        '"is not only open" reads as a denial',
        one(
            KNOWLEDGE_CORRECTNESS,
            'r"\\s+not\\b(?!\\s+(?:only|just)\\b)|',
            'r"\\s+not\\b|',
        ),
        (f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_a_right_answer_in_negative_words_stays_correct",),
    ),
    Mutation(
        "R2.10B-M99",
        "R2.10B",
        '"No it closes" reads as "No pool"',
        one(
            KNOWLEDGE_CORRECTNESS,
            "sooner|more|less|fewer|it|the|they|you|we|this|that|",
            "sooner|more|less|fewer|",
        ),
        (f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_a_right_answer_in_negative_words_stays_correct",),
    ),
    Mutation(
        "R2.10B-M100",
        "R2.10B",
        '"no more than" reads as a denial',
        one(
            KNOWLEDGE_CORRECTNESS,
            "sooner|more|less|fewer|it|",
            "sooner|it|",
        ),
        (f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_a_right_answer_in_negative_words_stays_correct",),
    ),
    Mutation(
        "R2.10B-M101",
        "R2.10B",
        "Not the pool is still the pool",
        one(
            KNOWLEDGE_CORRECTNESS,
            '    if _NOT_THIS_ONE.match(phrase):\n        return "other"\n',
            "",
        ),
        (f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_a_negated_name_is_not_the_one_named",),
    ),
    Mutation(
        "R2.10B-M102",
        "R2.10B",
        "a curly apostrophe hides a negated verb",
        one(
            KNOWLEDGE_CORRECTNESS,
            '.replace("\\u2019", "\'")',
            "",
        ),
        (f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_a_right_answer_in_negative_words_stays_correct",),
    ),
    Mutation(
        "R2.10B-M103",
        "R2.10B",
        "sentences do not limit what a negation covers",
        one(
            KNOWLEDGE_CORRECTNESS,
            "    for _, sentence_start, sentence_end in "
            "_pieces(_SENTENCE_END, text, 0, len(text)):\n"
            '        if text[sentence_end : sentence_end + 1] == "?":\n',
            '    for _, sentence_start, sentence_end in (("", 0, len(text)),):\n'
            '        if text[sentence_end : sentence_end + 1] == "?":\n',
        ),
        (
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_what_a_ne"
            f"gation_covers_is_read_one_sentence_at_a_time",
        ),
    ),
    Mutation(
        "R2.10B-M104",
        "R2.10B",
        "the negation rules are outside the checksum",
        one(
            KNOWLEDGE_CORRECTNESS,
            "                sorted(_SCOPE_ENDS),\n",
            "",
        ),
        (f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_a_change_to_a_negation_rule_changes_the_checksum",),
    ),
    Mutation(
        "R2.10B-M105",
        "R2.10B",
        "two negations read as one",
        one(
            KNOWLEDGE_CORRECTNESS,
            "            if cue is not None and _SECOND_NEGATION.search(words, cue.end()):\n",
            "            if False:\n",
        ),
        (f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_an_unclear_reach_is_not_assessable",),
    ),
    Mutation(
        "R2.10B-M106",
        "R2.10B",
        "and does not separate what a negation covers",
        one(
            KNOWLEDGE_CORRECTNESS,
            '    r"|\\b(?:and|but|while|whereas|although|though|however|which|'
            'whose|so|because|since)\\b",\n',
            '    r"|\\b(?:but|while|whereas|although|though|however|which|'
            'whose|so|because|since)\\b",\n',
        ),
        (f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_a_right_answer_in_negative_words_stays_correct",),
    ),
    Mutation(
        "R2.10B-M107",
        "R2.10B",
        "a question in passing is read as a statement",
        one(
            KNOWLEDGE_CORRECTNESS,
            '        if text[sentence_end : sentence_end + 1] == "?":\n',
            "        if False:\n",
        ),
        (
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_a_questio"
            f"n_in_passing_is_not_held_against_the_answer",
        ),
    ),
    Mutation(
        "R2.10B-M108",
        "R2.10B",
        "an answer that only asks is judged as a statement",
        one(
            KNOWLEDGE_CORRECTNESS,
            "    if _only_asks(_body(text)):\n",
            "    if False:\n",
        ),
        (
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_what_assert"
            f"s_nothing_is_neither_credited_nor_contradicted",
        ),
    ),
    Mutation(
        "R2.10B-M109",
        "R2.10B",
        "may not reads as does not",
        one(
            KNOWLEDGE_CORRECTNESS,
            "            if _HEDGED_NEGATION.search(words):\n",
            "            if False:\n",
        ),
        (
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_a_negati"
            f"on_that_only_says_might_not_decides_nothing",
        ),
    ),
    Mutation(
        "R2.10B-M110",
        "R2.10B",
        "may not reaches no further than its own words",
        one(
            KNOWLEDGE_CORRECTNESS,
            '                unclear.append((start, end))\n                scope = "adverb"\n',
            "                unclear.append((start, end))\n                scope = None\n",
        ),
        (
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_a_negati"
            f"on_that_only_says_might_not_decides_nothing",
        ),
    ),
    Mutation(
        "R2.10B-M111",
        "R2.10B",
        "a fragment reads as a closing time",
        one(
            KNOWLEDGE_CORRECTNESS,
            "            if fragment is not None and not _PREDICATE.search(words):\n",
            "            if False:\n",
        ),
        (
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_what_assert"
            f"s_nothing_is_neither_credited_nor_contradicted",
        ),
    ),
    Mutation(
        "R2.10B-M112",
        "R2.10B",
        "a verb before not until reads as a fragment",
        one(
            KNOWLEDGE_CORRECTNESS,
            "            if fragment is not None and not _PREDICATE.search(words):\n",
            "            if fragment is not None:\n",
        ),
        (
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_denying_a_wr"
            f"ong_alternative_leaves_the_right_fact_standing",
        ),
    ),
    Mutation(
        "R2.10B-M113",
        "R2.10B",
        "two negations before until read as an opening time",
        one(
            KNOWLEDGE_CORRECTNESS,
            "m.unclear_negation or m.role in",
            "m.role in",
        ),
        (
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_what_assert"
            f"s_nothing_is_neither_credited_nor_contradicted",
        ),
    ),
    Mutation(
        "R2.10B-M114",
        "R2.10B",
        "whole hours do not set another subject's time aside",
        one(
            KNOWLEDGE_CORRECTNESS,
            "    if any(m.whole_hours for m in mentions):\n        mentions = [m for m "
            'in mentions if m.subject != "other"]\n',
            "",
        ),
        (f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_the_required_audit_cases",),
    ),
    Mutation(
        "R2.10B-M115",
        "R2.10B",
        "a clause naming the subject and something else counts",
        one(
            KNOWLEDGE_CORRECTNESS,
            '            if names == {"assessed"} and {"opens", "closes"} <= roles:\n',
            '            if "assessed" in names and {"opens", "closes"} <= roles:\n',
        ),
        (f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_hours_not_given_whole_and_by_name_stay_undecided",),
    ),
    Mutation(
        "R2.10B-M116",
        "R2.10B",
        "a clause naming nothing counts as naming the subject",
        one(
            KNOWLEDGE_CORRECTNESS,
            '            if names == {"assessed"} and {"opens", "closes"} <= roles:\n',
            '            if "other" not in names and {"opens", "closes"} <= roles:\n',
        ),
        (f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_hours_not_given_whole_and_by_name_stay_undecided",),
    ),
    Mutation(
        "R2.10B-M117",
        "R2.10B",
        "half the hours count as whole",
        one(
            KNOWLEDGE_CORRECTNESS,
            '            if names == {"assessed"} and {"opens", "closes"} <= roles:\n',
            '            if names == {"assessed"} and roles:\n',
        ),
        (f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_hours_not_given_whole_and_by_name_stay_undecided",),
    ),
    Mutation(
        "R2.10B-M118",
        "R2.10B",
        "a denied end counts toward whole hours",
        one(
            KNOWLEDGE_CORRECTNESS,
            "            roles = {found[i].role for i in here if not found[i].denied}\n",
            "            roles = {found[i].role for i in here}\n",
        ),
        (f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_a_denied_end_is_not_whole_hours",),
    ),
    Mutation(
        "R2.10B-M119",
        "R2.10B",
        "a contrast does not end a clause",
        one(
            KNOWLEDGE_CORRECTNESS,
            "_CONTRAST = "
            're.compile(r"\\b(?:but|while|whereas|although|though|however)\\b", re.I)\n',
            '_CONTRAST = re.compile(r"$^")\n',
        ),
        (f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_the_required_audit_cases",),
    ),
    Mutation(
        "R2.10B-M120",
        "R2.10B",
        "whole hours keep the sentence's other subject",
        one(
            KNOWLEDGE_CORRECTNESS,
            'marked[i] = replace(found[i], subject="assessed", whole_hours=True)',
            "marked[i] = replace(found[i], whole_hours=True)",
        ),
        (f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_the_required_audit_cases",),
    ),
    Mutation(
        "R2.10B-M121",
        "R2.10B",
        "a new subject does not end what a negation covers",
        one(
            KNOWLEDGE_CORRECTNESS,
            "this|the|our|there|only",
            "this|there|only",
        ),
        (
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_another_"
            f"subjects_negation_stops_at_the_next_subject",
        ),
    ),
    Mutation(
        "R2.10B-M122",
        "R2.10B",
        "refusing the FAQ's wording no longer reports it",
        one(
            KNOWLEDGE_CORRECTNESS,
            '            reported = _REFUSAL.sub("I will", worded)\n',
            "            reported = worded\n",
        ),
        (f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_refusing_what_the_faq_says_still_reports_it",),
    ),
    Mutation(
        "R2.10B-M123",
        "R2.10B",
        "denying the FAQ says it still reports it",
        one(
            KNOWLEDGE_CORRECTNESS,
            "            standing = _standing(pattern, reported, negation_in(reported))\n",
            "            standing = _standing(pattern, reported, Negation((), ()))\n",
        ),
        (f"{KNOWLEDGE_CORRECTNESS_TESTS}::test_denying_that_the_faq_says_it_does_not_report_it",),
    ),
    Mutation(
        "R2.10B-M124",
        "R2.10B",
        "another subject's sentence counts as the same clause",
        one(
            KNOWLEDGE_CORRECTNESS,
            "    for _, sentence_start, sentence_end in "
            "_pieces(_SENTENCE_END, text, 0, len(text)):\n"
            "        for _, start, end in _pieces(_CONTRAST,",
            '    for _, sentence_start, sentence_end in (("", 0, len(text)),):\n'
            "        for _, start, end in _pieces(_CONTRAST,",
        ),
        (
            f"{KNOWLEDGE_CORRECTNESS_TESTS}::"
            "test_another_subjects_negation_takes_nothing_from_hours_given_by_name",
        ),
    ),
]

# --- R2.10C: retrieval metrics and the dataset schema, before any dataset ------------------------

RETRIEVAL_METRICS = "tests/evaluation/retrieval_metrics.py"
RETRIEVAL_METRICS_TESTS = "tests/evaluation/test_retrieval_metrics.py"
RETRIEVAL_SCHEMA = "tests/evaluation/retrieval_schema.py"
RETRIEVAL_SCHEMA_TESTS = "tests/evaluation/test_retrieval_schema.py"
RETRIEVAL_V2 = "tests/evaluation/retrieval_v2.py"
RETRIEVAL_V2_TESTS = "tests/evaluation/test_retrieval_v2.py"
_RM = f"{RETRIEVAL_METRICS_TESTS}::"
_RS = f"{RETRIEVAL_SCHEMA_TESTS}::"
_RV = f"{RETRIEVAL_V2_TESTS}::"
RETRIEVAL_REVIEW = "tests/evaluation/retrieval_review.py"
RETRIEVAL_REVIEW_TESTS = "tests/evaluation/test_retrieval_review.py"
_RR = f"{RETRIEVAL_REVIEW_TESTS}::"
MUTATION_HARNESS = "tests/mutation/harness.py"
_HT = "tests/mutation/test_harness.py::"
_DIFFERS = f"{_RV}test_a_dataset_that_differs_from_the_capture_is_reported"
_DRIFT = f"{_RV}test_a_copy_that_differs_from_its_source_is_reported"
_REFUSED = f"{_RS}test_a_malformed_or_inconsistent_dataset_is_refused"
_DENOMINATORS = f"{_RM}test_each_metric_has_its_own_denominator"
_NO_ANSWER = f"{_RM}test_strict_and_lenient_no_answer_checks_are_different_checks"
_AT_RANK = f"{_RM}test_a_sufficient_chunk_at_each_rank_inside_the_cutoff"
_WILSON = f"{_RM}test_wilson_intervals_match_their_known_values"


def _r210c(number: int, breaks: str, path: str, old: str, new: str, *killers: str) -> Mutation:
    return Mutation(f"R2.10C-M{number}", "R2.10C", breaks, one(path, old, new), killers)


STAGE_R2_10C = [
    _r210c(
        1,
        "the cut-off keeps one result too few",
        RETRIEVAL_METRICS,
        "    return ranked[:cutoff]\n",
        "    return ranked[: cutoff - 1]\n",
        f"{_AT_RANK}[5]",
    ),
    _r210c(
        2,
        "results past the cut-off are scored",
        RETRIEVAL_METRICS,
        "    return ranked[:cutoff]\n",
        "    return ranked\n",
        f"{_RM}test_a_sufficient_chunk_at_rank_six_is_a_miss",
    ),
    _r210c(
        3,
        "Success@5 counts a grade-1 chunk as sufficient",
        RETRIEVAL_METRICS,
        "    return any(query.grade_of(chunk) >= SUFFICIENT for chunk in _top(ranked, cutoff))\n",
        "    return any(query.grade_of(chunk) >= 1 for chunk in _top(ranked, cutoff))\n",
        f"{_RM}test_grade_1_and_grade_2_are_not_the_same_to_success_or_recall",
    ),
    _r210c(
        4,
        "Recall@5 divides by the cut-off, not by the query's grade-2 chunks",
        RETRIEVAL_METRICS,
        "    return Fraction(len(found), len(required))\n",
        "    return Fraction(len(found), cutoff)\n",
        f"{_RM}test_partial_retrieval_of_two_sufficient_chunks",
        f"{_RM}test_perfect_retrieval_scores_one_everywhere",
    ),
    _r210c(
        5,
        "Recall@5 counts every judged chunk, grade 1 and 0 included",
        RETRIEVAL_METRICS,
        "    required = query.sufficient\n",
        "    required = frozenset(item.ref for item in query.relevant)\n",
        f"{_RM}test_grade_1_and_grade_2_are_not_the_same_to_success_or_recall",
    ),
    _r210c(
        6,
        "ranks start at 2, so every reciprocal rank is one place too low",
        RETRIEVAL_METRICS,
        "    for rank, chunk in enumerate(_top(ranked, cutoff), start=1):\n"
        "        if query.grade_of(chunk) >= min_grade:\n",
        "    for rank, chunk in enumerate(_top(ranked, cutoff), start=2):\n"
        "        if query.grade_of(chunk) >= min_grade:\n",
        f"{_AT_RANK}[1]",
        f"{_AT_RANK}[5]",
        f"{_RM}test_the_mrr_threshold_is_the_configured_one",
    ),
    _r210c(
        7,
        "MRR@5 ignores its configured grade and counts grade 2 only",
        RETRIEVAL_METRICS,
        '    rank = first_rank_at(query, ranked, cutoff, _threshold(min_grade, "min_grade"))\n',
        "    rank = first_rank_at(query, ranked, cutoff, SUFFICIENT)\n",
        f"{_RM}test_the_mrr_threshold_is_the_configured_one",
    ),
    _r210c(
        8,
        "nDCG@5 uses the grade itself, whatever gains were configured",
        RETRIEVAL_METRICS,
        "    actual = [gains[query.grade_of(chunk)] for chunk in _top(ranked, cutoff)]\n",
        "    actual = [query.grade_of(chunk) for chunk in _top(ranked, cutoff)]\n",
        f"{_RM}test_the_gains_are_the_configured_ones",
    ),
    _r210c(
        9,
        "the ideal ordering puts the lowest grade first",
        RETRIEVAL_METRICS,
        "for item in query.relevant), reverse=True)[:cutoff]\n",
        "for item in query.relevant), reverse=False)[:cutoff]\n",
        f"{_RM}test_the_ideal_ordering_puts_the_highest_grade_first",
        f"{_RM}test_perfect_retrieval_scores_one_everywhere",
    ),
    _r210c(
        10,
        "the discount is log2(rank + 2): one rank off",
        RETRIEVAL_METRICS,
        "    return math.fsum(gain / math.log2(rank + 1) for rank,",
        "    return math.fsum(gain / math.log2(rank + 2) for rank,",
        f"{_AT_RANK}[1]",
        f"{_RM}test_the_gains_are_the_configured_ones",
    ),
    _r210c(
        11,
        "an out-of-scope chunk past the cut-off is not leakage",
        RETRIEVAL_METRICS,
        "    for chunk in ranked:\n        document = documents.get(chunk.doc_id)\n",
        "    for chunk in ranked[:5]:\n        document = documents.get(chunk.doc_id)\n",
        f"{_RM}test_leakage_is_found_past_the_cutoff",
    ),
    _r210c(
        12,
        "leakage is never detected",
        RETRIEVAL_METRICS,
        "            found.append(chunk)\n",
        "            pass\n",
        f"{_RM}test_a_neighbour_chunk_leaks_even_when_no_forbidden_list_names_it",
        f"{_DENOMINATORS}",
    ),
    _r210c(
        13,
        "the strict no-answer check is the lenient one",
        RETRIEVAL_METRICS,
        "    return not ranked\n",
        "    return not any(query.grade_of(chunk) >= 1 for chunk in ranked)\n",
        f"{_NO_ANSWER}[grade-0]",
    ),
    _r210c(
        14,
        "the lenient no-answer check lets its own threshold grade through",
        RETRIEVAL_METRICS,
        "    return not any(query.grade_of(chunk) >= threshold for chunk in ranked)\n",
        "    return not any(query.grade_of(chunk) > threshold for chunk in ranked)\n",
        f"{_NO_ANSWER}[grade-1]",
    ),
    _r210c(
        15,
        "the lenient no-answer check reads only the top five",
        RETRIEVAL_METRICS,
        "    return not any(query.grade_of(chunk) >= threshold for chunk in ranked)\n",
        "    return not any(query.grade_of(chunk) >= threshold for chunk in ranked[:5])\n",
        f"{_NO_ANSWER}[grade-1-past-the-cutoff]",
    ),
    _r210c(
        16,
        "Success@5 is divided by every query, no-answer ones included",
        RETRIEVAL_METRICS,
        "            sum(bool(item.success) for item in answerable), len(answerable)\n",
        "            sum(bool(item.success) for item in answerable), len(outcomes)\n",
        _DENOMINATORS,
    ),
    _r210c(
        17,
        "the empty-result rate counts no-answer queries",
        RETRIEVAL_METRICS,
        '        "empty_result_rate": _proportion(sum(item.empty for item in answerable), '
        "len(answerable)),\n",
        '        "empty_result_rate": _proportion(sum(item.empty for item in outcomes), '
        "len(outcomes)),\n",
        _DENOMINATORS,
    ),
    _r210c(
        18,
        "no-answer queries enter Recall@5 as zeros",
        RETRIEVAL_METRICS,
        '        "recall_at_5": _mean_exact([item.recall for item in answerable '
        "if item.recall is not None]),\n",
        '        "recall_at_5": _mean_exact([item.recall or Fraction(0) for item in outcomes]),\n',
        _DENOMINATORS,
    ),
    _r210c(
        19,
        "every family is reported with every query",
        RETRIEVAL_METRICS,
        "            family: aggregate([item for item in outcomes if item.family == family])\n",
        "            family: aggregate(list(outcomes))\n",
        f"{_RM}test_metrics_are_reported_per_family",
    ),
    _r210c(
        20,
        "the Wilson interval uses the 90% quantile",
        RETRIEVAL_METRICS,
        "WILSON_Z_95: Final = 1.959963984540054\n",
        "WILSON_Z_95: Final = 1.6448536269514722\n",
        _WILSON + "[5-10-expected2]",
        f"{_RM}test_the_report_records_its_configuration",
    ),
    _r210c(
        21,
        "the Wilson half-width drops its z^2 / 4n^2 term",
        RETRIEVAL_METRICS,
        "    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator\n",
        "    half = z * math.sqrt(p * (1 - p) / n) / denominator\n",
        _WILSON + "[5-10-expected2]",
        _WILSON + "[16-23-expected3]",
    ),
    _r210c(
        22,
        "the Wilson interval is centred on the raw proportion",
        RETRIEVAL_METRICS,
        "    centre = (p + z * z / (2 * n)) / denominator\n",
        "    centre = p\n",
        _WILSON + "[16-23-expected3]",
    ),
    _r210c(
        23,
        "a chunk ranked twice is accepted",
        RETRIEVAL_METRICS,
        "        if len(set(chunks)) != len(chunks):\n",
        "        if False:\n",
        f"{_RM}test_invalid_results_are_refused[duplicate]",
        f"{_RM}test_invalid_results_are_refused[duplicate-past-the-cutoff]",
    ),
    _r210c(
        24,
        "a result that is no chunk of the corpus is accepted",
        RETRIEVAL_METRICS,
        "            if chunk not in corpus:\n",
        "            if False:\n",
        f"{_RM}test_invalid_results_are_refused[unknown-ordinal]",
        f"{_RM}test_invalid_results_are_refused[unknown-document]",
    ),
    _r210c(
        25,
        "a no-answer query is scored as if it had an answer",
        RETRIEVAL_METRICS,
        "    if not query.answerable:\n"
        '        raise ValueError(f"{query.query_id} is a no-answer '
        'query; it has no score here")\n',
        "    if False:\n"
        '        raise ValueError(f"{query.query_id} is a no-answer '
        'query; it has no score here")\n',
        f"{_RM}test_answerable_and_no_answer_functions_refuse_each_others_queries",
    ),
    _r210c(
        26,
        "grade 0 may gain, so an irrelevant chunk raises nDCG",
        RETRIEVAL_METRICS,
        "        if gains[0] != 0:\n",
        "        if False:\n",
        f"{_RM}test_a_malformed_configuration_is_refused[grade-0-gains]",
    ),
    _r210c(
        27,
        "grade 1 may gain more than grade 2",
        RETRIEVAL_METRICS,
        "        if not 0 <= gains[1] <= gains[2] or gains[2] <= 0:\n",
        "        if gains[2] <= 0:\n",
        f"{_RM}test_a_malformed_configuration_is_refused[grade-1-above-grade-2]",
        f"{_RM}test_a_malformed_configuration_is_refused[negative-gain]",
    ),
    _r210c(
        28,
        "the schema accepts grade 3",
        RETRIEVAL_SCHEMA,
        "GRADES: Final = (0, 1, 2)\n",
        "GRADES: Final = (0, 1, 2, 3)\n",
        f"{_REFUSED}[grade-3]",
    ),
    _r210c(
        29,
        "an answerable query needs no grade-2 chunk",
        RETRIEVAL_SCHEMA,
        "        if self.answerable and not sufficient:\n",
        "        if False:\n",
        f"{_REFUSED}[an-answerable-query-with-no-grade-2-chunk]",
        f"{_RS}test_construction_is_validated_like_loading",
    ),
    _r210c(
        30,
        "a no-answer query may hold a grade-2 chunk",
        RETRIEVAL_SCHEMA,
        "        if not self.answerable and sufficient:\n",
        "        if False:\n",
        f"{_REFUSED}[a-no-answer-query-with-a-grade-2-chunk]",
    ),
    _r210c(
        31,
        "an unknown field is silently ignored",
        RETRIEVAL_SCHEMA,
        "    if unknown:\n",
        "    if False:\n",
        f"{_REFUSED}[unknown-top-level-field]",
        f"{_REFUSED}[unknown-query-field]",
    ),
    _r210c(
        32,
        "true and false pass as integers",
        RETRIEVAL_SCHEMA,
        "    if isinstance(value, bool) or not isinstance(value, int):\n"
        '        raise DatasetError(f"{where} must be an integer")\n',
        "    if not isinstance(value, int):\n"
        '        raise DatasetError(f"{where} must be an integer")\n',
        f"{_REFUSED}[a-boolean-grade]",
        f"{_REFUSED}[a-boolean-cutoff]",
    ),
    _r210c(
        33,
        "a judged chunk may lie out of scope",
        RETRIEVAL_SCHEMA,
        "                if not self._resolve(documents, chunk, where).in_scope:\n",
        "                if not self._resolve(documents, chunk, where).in_scope and False:\n",
        f"{_REFUSED}[a-judged-chunk-out-of-scope]",
        f"{_REFUSED}[a-judged-inactive-version]",
    ),
    _r210c(
        34,
        "a forbidden chunk may lie in scope",
        RETRIEVAL_SCHEMA,
        "                if self._resolve(documents, chunk, where).in_scope:\n",
        "                if self._resolve(documents, chunk, where).in_scope and False:\n",
        f"{_REFUSED}[a-forbidden-chunk-in-scope]",
    ),
    _r210c(
        35,
        "the neighbour hotel's active documents count as in scope",
        RETRIEVAL_SCHEMA,
        '        return self.hotel == "evaluated" and self.status == "active"\n',
        '        return self.status == "active"\n',
        f"{_RS}test_a_valid_dataset_loads_with_everything_it_declared",
    ),
    _r210c(
        36,
        "the canonical form keeps keys in insertion order",
        RETRIEVAL_SCHEMA,
        '            self.as_mapping(), sort_keys=True, separators=(",", '
        '":"), ensure_ascii=False\n',
        '            self.as_mapping(), sort_keys=False, separators=(",", '
        '":"), ensure_ascii=False\n',
        f"{_RS}test_the_checksum_is_pinned",
        f"{_RS}test_the_canonical_json_has_sorted_keys_and_compact_separators",
    ),
    _r210c(
        37,
        "the canonical form escapes non-ASCII text",
        RETRIEVAL_SCHEMA,
        '            self.as_mapping(), sort_keys=True, separators=(",", '
        '":"), ensure_ascii=False\n',
        '            self.as_mapping(), sort_keys=True, separators=(",", ":"), ensure_ascii=True\n',
        f"{_RS}test_the_checksum_is_pinned",
        f"{_RS}test_the_canonical_json_has_sorted_keys_and_compact_separators",
    ),
    _r210c(
        38,
        "judgments keep their authoring order, so reordering them changes the checksum",
        RETRIEVAL_SCHEMA,
        '            self, "relevant", tuple(sorted(self.relevant, key=lambda item: item.ref))\n',
        '            self, "relevant", tuple(self.relevant)\n',
        f"{_RS}test_the_checksum_ignores_the_order_of_keys_and_of_judgments",
        f"{_RS}test_the_judgment_lists_are_stored_sorted",
    ),
    _r210c(
        39,
        "a duplicate JSON key silently replaces the first",
        RETRIEVAL_SCHEMA,
        "    if duplicated:\n",
        "    if False:\n",
        f"{_RS}test_duplicate_json_keys_are_refused_not_resolved",
    ),
    _r210c(
        40,
        "a model-captured query needs no provenance",
        RETRIEVAL_SCHEMA,
        '        if (self.origin == "model_captured") != (self.provenance is not None):\n',
        "        if False:\n",
        f"{_REFUSED}[model_captured-with-no-provenance]",
        f"{_REFUSED}[a-developer-query-with-provenance]",
    ),
    # R2.10C.2.1: corpus-derived leakage, the grade-1-only lenient check, the bootstrap.
    _r210c(
        41,
        "leakage ignores status, so a superseded or withdrawn version passes",
        RETRIEVAL_METRICS,
        '        if document.hotel != "evaluated" or document.status != "active":\n',
        '        if document.hotel != "evaluated":\n',
        f"{_RM}test_a_superseded_version_leaks",
        f"{_RM}test_a_withdrawn_document_leaks",
    ),
    _r210c(
        42,
        "leakage ignores the hotel, so the neighbour's active document passes",
        RETRIEVAL_METRICS,
        '        if document.hotel != "evaluated" or document.status != "active":\n',
        '        if document.status != "active":\n',
        f"{_RM}test_a_neighbour_chunk_leaks_even_when_no_forbidden_list_names_it",
        f"{_RM}test_emptying_every_forbidden_list_cannot_pass_the_leakage_gate",
    ),
    _r210c(
        43,
        "leakage is read from the query's forbidden list again",
        RETRIEVAL_METRICS,
        "        leaked=leaked(dataset, ranked),\n",
        "        leaked=trap_hits(query, ranked),\n",
        f"{_RM}test_a_neighbour_chunk_leaks_even_when_no_forbidden_list_names_it",
        f"{_RM}test_emptying_every_forbidden_list_cannot_pass_the_leakage_gate",
    ),
    _r210c(
        44,
        "leakage counts leaking chunks, not leaking queries",
        RETRIEVAL_METRICS,
        '        "scope_leakage": _proportion(sum(bool(item.leaked) for item in outcomes), '
        "len(outcomes)),\n",
        '        "scope_leakage": _proportion(sum(len(item.leaked) for item in outcomes), '
        "len(outcomes)),\n",
        f"{_RM}test_a_query_with_several_leaking_chunks_counts_once",
    ),
    _r210c(
        45,
        "no-answer queries are left out of the leakage rate",
        RETRIEVAL_METRICS,
        '        "scope_leakage": _proportion(sum(bool(item.leaked) for item in outcomes), '
        "len(outcomes)),\n",
        '        "scope_leakage": _proportion(sum(bool(item.leaked) for item in answerable), '
        "len(answerable)),\n",
        f"{_RM}test_no_answer_queries_are_in_the_leakage_denominator",
    ),
    _r210c(
        46,
        "the lenient no-answer check accepts grade 2, which can never fail",
        RETRIEVAL_METRICS,
        "    if isinstance(value, bool) or not isinstance(value, int) or "
        "value != LENIENT_MIN_GRADE:\n",
        "    if isinstance(value, bool) or not isinstance(value, int) or value not in (1, 2):\n",
        f"{_RM}test_a_malformed_configuration_is_refused[lenient-2]",
        f"{_RM}test_a_threshold_passed_directly_is_checked_too",
    ),
    _r210c(
        47,
        "the bootstrap ignores its seed",
        RETRIEVAL_METRICS,
        "    generator = random.Random(seed)\n",
        "    generator = random.Random()\n",
        f"{_RM}test_the_same_seed_gives_the_same_interval",
        f"{_RM}test_another_seed_is_supported_and_recorded",
    ),
    _r210c(
        48,
        "the bootstrap ignores the resample count it was given",
        RETRIEVAL_METRICS,
        "        for _ in range(resamples)\n",
        "        for _ in range(BOOTSTRAP_RESAMPLES)\n",
        f"{_RM}test_another_seed_is_supported_and_recorded",
    ),
    _r210c(
        49,
        "the lower percentile is one rank too high",
        RETRIEVAL_METRICS,
        "    return ordered[low_rank - 1], ordered[high_rank - 1]\n",
        "    return ordered[low_rank], ordered[high_rank - 1]\n",
        f"{_RM}test_the_percentiles_are_the_nearest_ranks",
    ),
    _r210c(
        50,
        "the upper percentile is one rank too high",
        RETRIEVAL_METRICS,
        "    return ordered[low_rank - 1], ordered[high_rank - 1]\n",
        "    return ordered[low_rank - 1], ordered[high_rank]\n",
        f"{_RM}test_the_percentiles_are_the_nearest_ranks",
    ),
    _r210c(
        51,
        "fewer than ten queries still get an interval",
        RETRIEVAL_METRICS,
        "    if n < min_n:\n",
        "    if n < 1:\n",
        f"{_RM}test_below_ten_queries_there_is_no_interval",
    ),
    _r210c(
        52,
        "a zero-width interval is never flagged",
        RETRIEVAL_METRICS,
        '    block["zero_width"] = low == high\n',
        '    block["zero_width"] = False\n',
        f"{_RM}test_a_zero_width_interval_is_flagged",
    ),
    _r210c(
        53,
        "the bootstrap does not resample: every resample is the sample itself",
        RETRIEVAL_METRICS,
        "        math.fsum(observations[generator.randrange(n)] for _ in range(n)) / n\n",
        "        math.fsum(observations) / n\n",
        f"{_RM}test_the_same_seed_gives_the_same_interval",
        f"{_RM}test_the_interval_lies_around_the_mean_and_inside_the_values",
    ),
    _r210c(
        54,
        "a mean is resampled over more observations than it has queries",
        RETRIEVAL_METRICS,
        '        "value": round(float(sum(values) / len(values)), DIGITS) if values else None,\n'
        '        "bootstrap_95": bootstrap_interval(list(values)),\n',
        '        "value": round(float(sum(values) / len(values)), DIGITS) if values else None,\n'
        '        "bootstrap_95": bootstrap_interval(list(values) * 2),\n',
        f"{_RM}test_the_resampling_unit_is_the_query_not_the_chunk",
        f"{_RM}test_rates_carry_a_wilson_interval_and_means_a_bootstrap_block",
    ),
    # R2.10C.3a: schema v2 (sources, versions, limits, occurrence provenance), the approved config.
    _r210c(
        55,
        "a document may supersede a version that is not superseded",
        RETRIEVAL_SCHEMA,
        '            if target.status != "superseded":\n',
        "            if False:\n",
        f"{_REFUSED}[a-successor-of-an-active-version]",
    ),
    _r210c(
        56,
        "a superseded version may have no successor",
        RETRIEVAL_SCHEMA,
        '            if document.status == "superseded" and document.doc_id not in successors:\n',
        "            if False:\n",
        f"{_REFUSED}[a-superseded-version-without-a-successor]",
    ),
    _r210c(
        57,
        "a new version may come before the version it is made from",
        RETRIEVAL_SCHEMA,
        "            if position[target.doc_id] >= position[document.doc_id]:\n",
        "            if False:\n",
        f"{_REFUSED}[a-new-version-before-the-one-it-supersedes]",
    ),
    _r210c(
        58,
        "a version may be superseded twice",
        RETRIEVAL_SCHEMA,
        "            if target.doc_id in successors:\n",
        "            if False:\n",
        f"{_REFUSED}[a-version-superseded-twice]",
    ),
    _r210c(
        59,
        "a version may be superseded from another hotel",
        RETRIEVAL_SCHEMA,
        "            if target.hotel != document.hotel:\n",
        "            if False:\n",
        f"{_REFUSED}[a-sole-successor-in-another-hotel]",
    ),
    Mutation(
        "R2.10C-M60",
        "R2.10C",
        "a document may supersede itself",
        one(
            RETRIEVAL_SCHEMA,
            "            if target.doc_id == document.doc_id:\n",
            "            if False:\n",
        ),
        (f"{_REFUSED}[supersedes-itself]",),
        note=(
            "Diagnostic by design: without this check a self-supersession is still refused, by the "
            "status or the order rule. The killer asserts the explicit message, which is the "
            "check's purpose (R2.10C.3a remediation, F2)."
        ),
    ),
    _r210c(
        61,
        "a limit above the search route's maximum is accepted",
        RETRIEVAL_SCHEMA,
        "    if not 1 <= limit <= MAX_LIMIT:\n",
        "    if not 1 <= limit:\n",
        f"{_REFUSED}[a-limit-above-20]",
    ),
    _r210c(
        62,
        "a limit of 0 is accepted",
        RETRIEVAL_SCHEMA,
        "    if not 1 <= limit <= MAX_LIMIT:\n",
        "    if not limit <= MAX_LIMIT:\n",
        f"{_REFUSED}[a-limit-of-0]",
    ),
    _r210c(
        63,
        "the provenance limit may differ from the query's",
        RETRIEVAL_SCHEMA,
        "        if self.provenance is not None and self.provenance.limit != self.limit:\n",
        "        if False:\n",
        f"{_REFUSED}[a-provenance-limit-that-differs]",
    ),
    _r210c(
        64,
        "an occurrence may be recorded twice",
        RETRIEVAL_SCHEMA,
        "        if len(set(self.occurrences)) != len(self.occurrences):\n",
        "        if False:\n",
        f"{_REFUSED}[an-occurrence-recorded-twice]",
    ),
    _r210c(
        65,
        "a provenance may record no occurrence",
        RETRIEVAL_SCHEMA,
        "        if not isinstance(self.occurrences, tuple) or not self.occurrences:\n",
        "        if not isinstance(self.occurrences, tuple):\n",
        f"{_REFUSED}[no-occurrences]",
    ),
    _r210c(
        66,
        "attempt 0 is accepted",
        RETRIEVAL_SCHEMA,
        '        if _integer(self.attempt, "occurrence attempt") < 1:\n',
        '        if _integer(self.attempt, "occurrence attempt") < 0:\n',
        f"{_REFUSED}[attempt-0]",
    ),
    _r210c(
        67,
        "a run key loses its @, so a captured run cannot be recorded as captured",
        RETRIEVAL_SCHEMA,
        '        _identifier(self.run, _RUN, "occurrence run")\n',
        '        _identifier(self.run, _ID, "occurrence run")\n',
        f"{_RS}test_a_run_key_keeps_its_at_sign_as_captured",
    ),
    _r210c(
        68,
        "occurrences keep their authoring order, so reordering them changes the checksum",
        RETRIEVAL_SCHEMA,
        '        object.__setattr__(self, "occurrences", tuple(sorted(self.occurrences)))\n',
        '        object.__setattr__(self, "occurrences", tuple(self.occurrences))\n',
        f"{_RS}test_occurrences_are_stored_sorted",
    ),
    _r210c(
        69,
        "a source digest need not match the copied text",
        RETRIEVAL_SCHEMA,
        "        if self.source.text_sha256 != content_sha256(self.paragraphs):\n",
        "        if False:\n",
        f"{_REFUSED}[a-source-digest-that-does-not-match-the-text]",
    ),
    _r210c(
        70,
        "a capture file may be any text, an absolute path included",
        RETRIEVAL_SCHEMA,
        '        _identifier(self.capture_file, _PATH, "provenance capture_file")\n',
        '        _text(self.capture_file, "provenance capture_file")\n',
        f"{_REFUSED}[a-malformed-capture-file]",
    ),
    _r210c(
        71,
        "a source module may be any text, a parent or absolute path included",
        RETRIEVAL_SCHEMA,
        '        _identifier(self.module, _PATH, "source module")\n',
        '        _text(self.module, "source module")\n',
        f"{_REFUSED}[a-malformed-source-module]",
        f"{_REFUSED}[an-absolute-source-module]",
    ),
    _r210c(
        72,
        "a ranking longer than the query's limit is accepted",
        RETRIEVAL_METRICS,
        "        if len(items) > limits[query_id]:\n",
        "        if False:\n",
        f"{_RM}test_invalid_results_are_refused[more-than-its-limit]",
    ),
    _r210c(
        73,
        "the approved configuration counts grade 1 for MRR@5",
        RETRIEVAL_METRICS,
        "    mrr_min_grade=2, ndcg_gains=(0, 1, 3), no_answer_lenient_min_grade=1\n",
        "    mrr_min_grade=1, ndcg_gains=(0, 1, 3), no_answer_lenient_min_grade=1\n",
        f"{_RM}test_the_approved_v2_configuration",
        f"{_RM}test_under_the_v2_configuration_mrr_is_positive_exactly_when_success_holds[grade-1]",
    ),
    _r210c(
        74,
        "the approved configuration uses linear gains",
        RETRIEVAL_METRICS,
        "    mrr_min_grade=2, ndcg_gains=(0, 1, 3), no_answer_lenient_min_grade=1\n",
        "    mrr_min_grade=2, ndcg_gains=(0, 1, 2), no_answer_lenient_min_grade=1\n",
        f"{_RM}test_the_approved_v2_configuration",
    ),
    # R2.10C.3b: the knowledge_retrieval_v2 snapshot, its sources and the R2.9Y capture.
    _r210c(
        75,
        "a snapshot whose checksum is not the pinned one is loaded",
        RETRIEVAL_V2,
        '    if actual != expected_sha256:\n        raise SnapshotError(f"snapshot checksum',
        '    if False:\n        raise SnapshotError(f"snapshot checksum',
        f"{_RV}test_a_changed_snapshot_is_refused",
    ),
    _r210c(
        76,
        "a capture whose bytes are not the pinned ones is read",
        RETRIEVAL_V2,
        '    if actual != expected_sha256:\n        raise SnapshotError(f"capture SHA-256',
        '    if False:\n        raise SnapshotError(f"capture SHA-256',
        f"{_RV}test_the_capture_is_the_pinned_one",
    ),
    _r210c(
        77,
        "a search repeated within one attempt is merged instead of refused",
        RETRIEVAL_V2,
        "                    if occurrence in seen:\n",
        "                    if False:\n",
        f"{_RV}test_a_repeat_within_one_attempt_is_refused_not_merged",
    ),
    _r210c(
        78,
        "captured searches are grouped without their limit",
        RETRIEVAL_V2,
        "                    seen = groups.setdefault((case, query, limit), [])\n",
        "                    seen = groups.setdefault((case, query, 5), [])\n",
        f"{_RV}test_the_captured_ids_follow_first_occurrence",
        f"{_RV}test_the_dataset_holds_exactly_the_captured_queries",
    ),
    _r210c(
        79,
        "captured ids are numbered against first-occurrence order",
        RETRIEVAL_V2,
        '    return f"r29y.{item.case}.{same_case.index(item) + 1}"\n',
        '    return f"r29y.{item.case}.{len(same_case) - same_case.index(item)}"\n',
        f"{_RV}test_the_captured_ids_follow_first_occurrence",
    ),
    _r210c(
        80,
        "occurrences that differ from the capture go unreported",
        RETRIEVAL_V2,
        "        if recorded != sorted(item.occurrences):\n",
        "        if False:\n",
        f"{_DIFFERS}[dropped-occurrence]",
        f"{_DIFFERS}[changed-run]",
    ),
    _r210c(
        81,
        "a limit that differs from the capture goes unreported",
        RETRIEVAL_V2,
        "        if query.limit != item.limit or provenance.limit != item.limit:\n",
        "        if False:\n",
        f"{_DIFFERS}[changed-limit]",
    ),
    _r210c(
        82,
        "a captured query missing from the dataset goes unreported",
        RETRIEVAL_V2,
        "    for query_id in sorted(set(expected) - set(actual_ids)):\n",
        "    for query_id in []:\n",
        f"{_DIFFERS}[missing-query]",
        f"{_RV}test_a_search_the_dataset_lacks_is_reported",
    ),
    _r210c(
        83,
        "a dataset query the capture lacks goes unreported",
        RETRIEVAL_V2,
        "    for query_id in sorted(set(actual_ids) - set(expected)):\n",
        "    for query_id in []:\n",
        f"{_DIFFERS}[extra-query]",
    ),
    _r210c(
        84,
        "a provenance naming another capture checksum goes unreported",
        RETRIEVAL_V2,
        "        if provenance.capture_sha256 != capture_sha256:\n",
        "        if False:\n",
        f"{_DIFFERS}[wrong-capture-sha]",
    ),
    _r210c(
        85,
        "a provenance naming another capture file goes unreported",
        RETRIEVAL_V2,
        "        if provenance.capture_file != CAPTURE_FILE:\n",
        "        if False:\n",
        f"{_DIFFERS}[wrong-capture-file]",
    ),
    _r210c(
        86,
        "a copy whose fields differ from its source goes unreported",
        RETRIEVAL_V2,
        "            if getattr(document, field) != getattr(source, field):\n",
        "            if False:\n",
        f"{_DRIFT}[title]",
        f"{_DRIFT}[status]",
    ),
    _r210c(
        87,
        "every fixture document is read as the evaluated hotel's",
        RETRIEVAL_V2,
        '    hotel = "neighbour" if item.hotel == NEIGHBOUR_HOTEL else "evaluated"\n',
        '    hotel = "evaluated"\n',
        f"{_RV}test_every_document_matches_its_source",
    ),
    _r210c(
        88,
        "every fixture document is read as active",
        RETRIEVAL_V2,
        '    return SourceDocument(item.title, "english", item.status, hotel, (item.text,))\n',
        '    return SourceDocument(item.title, "english", "active", hotel, (item.text,))\n',
        f"{_RV}test_every_document_matches_its_source",
    ),
    _r210c(
        89,
        "the snapshot is rendered without sorted keys",
        RETRIEVAL_V2,
        "indent=2, sort_keys=True, ensure_ascii=False) + ",
        "indent=2, sort_keys=False, ensure_ascii=False) + ",
        f"{_RV}test_the_file_is_exactly_its_canonical_rendering",
    ),
    _r210c(
        90,
        "the capture is hashed as checked out, so a CRLF checkout is refused",
        RETRIEVAL_V2,
        '    content = raw.replace(b"\\r\\n", b"\\n")\n',
        "    content = raw\n",
        f"{_RV}test_a_crlf_checkout_of_the_capture_is_the_same_capture",
    ),
    # R2.10C.3b: the harness batches pytest killers (WinError 206).
    _r210c(
        91,
        "the last, partly filled batch of killers is dropped",
        MUTATION_HARNESS,
        "    if current:\n        batches.append(current)\n",
        "",
        f"{_HT}test_killers_are_split_into_batches_within_the_bound",
        f"{_HT}test_no_killers_make_no_batch_and_a_few_make_one",
    ),
    _r210c(
        92,
        "each batch's verdicts replace the earlier batches' instead of joining them",
        MUTATION_HARNESS,
        "                results.update(parse_pytest_summary(completed.stdout, batch))\n",
        "                results = parse_pytest_summary(completed.stdout, batch)\n",
        f"{_HT}test_verdicts_are_gathered_from_every_batch",
    ),
    _r210c(
        93,
        "the database is rebuilt only before the first batch",
        MUTATION_HARNESS,
        '                    run_in_copy(copy, env, RESET_DATABASE, "could '
        'not reset the test database")\n',
        "                    if not results:\n"
        '                        run_in_copy(copy, env, RESET_DATABASE, "could '
        'not reset the test database")\n',
        f"{_HT}test_a_database_backed_run_is_rebuilt_before_every_batch",
    ),
    # R2.10C.3c: the review sheet and the review form.
    _r210c(
        94,
        "a form made for another dataset checksum is accepted",
        RETRIEVAL_REVIEW,
        '            if form["dataset"][field] != value:\n',
        "            if False:\n",
        f"{_RR}test_a_form_claiming_another_checksum_is_refused",
        f"{_RR}test_a_review_of_another_dataset_is_refused",
    ),
    _r210c(
        95,
        "a form made against another review sheet is accepted",
        RETRIEVAL_REVIEW,
        '    if form["review_sheet_sha256"] != review_sheet_sha256:\n',
        "    if False:\n",
        f"{_RR}test_a_review_against_another_sheet_is_refused",
    ),
    _r210c(
        96,
        "a changed grade needs no rationale",
        RETRIEVAL_REVIEW,
        "        if not _explained(rationale):\n"
        '            problems.append(f"{where}: a change needs a rationale")\n',
        '        if False:\n            problems.append(f"{where}: a change needs a rationale")\n',
        f"{_RR}test_a_malformed_review_is_refused[change-no-rationale]",
        f"{_RR}test_a_malformed_review_is_refused[change-blank-rationale]",
    ),
    _r210c(
        97,
        "a 'change' to the proposed grade is accepted",
        RETRIEVAL_REVIEW,
        "        elif grade == proposed[1]:\n",
        "        elif False:\n",
        f"{_RR}test_a_malformed_review_is_refused[same-grade]",
    ),
    _r210c(
        98,
        "an undecided judgment is not reported as outstanding",
        RETRIEVAL_REVIEW,
        '            if judgment["decision"] is None:\n',
        "            if False:\n",
        f"{_RR}test_a_partial_review_lists_exactly_what_is_outstanding",
        f"{_RR}test_a_blank_form_is_not_a_review",
    ),
    _r210c(
        99,
        "an added judgment may name an out-of-scope chunk",
        RETRIEVAL_REVIEW,
        "    elif not found[0].in_scope:\n",
        "    elif False:\n",
        f"{_RR}test_a_malformed_review_is_refused[added-out-of-scope]",
        f"{_RR}test_a_malformed_review_is_refused[added-neighbour]",
    ),
    _r210c(
        100,
        "the sheet is hashed as checked out, so a CRLF checkout is another sheet",
        RETRIEVAL_REVIEW,
        '    return hashlib.sha256(text.replace("\\r\\n", "\\n").encode("utf-8")).hexdigest()\n',
        '    return hashlib.sha256(text.encode("utf-8")).hexdigest()\n',
        f"{_RR}test_a_crlf_checkout_of_the_sheet_is_the_same_sheet",
    ),
    _r210c(
        101,
        "true is accepted as grade 1",
        RETRIEVAL_REVIEW,
        "    return isinstance(value, int) and not isinstance(value, bool) and value in GRADES\n",
        "    return isinstance(value, int) and value in GRADES\n",
        f"{_RR}test_a_malformed_review_is_refused[grade-bool]",
        f"{_RR}test_a_malformed_review_is_refused[added-grade-bool]",
    ),
    _r210c(
        102,
        "a review date that is no calendar date is accepted",
        RETRIEVAL_REVIEW,
        "                dt.date.fromisoformat(str(reviewed_on))\n",
        "                pass\n",
        f"{_RR}test_a_malformed_review_is_refused[bad-date]",
    ),
    _r210c(
        103,
        "a chunk may be added at grade 0",
        RETRIEVAL_REVIEW,
        "    if not _is_grade(grade) or grade == 0:\n",
        "    if not _is_grade(grade):\n",
        f"{_RR}test_a_malformed_review_is_refused[added-grade-0]",
    ),
    _r210c(
        104,
        "a chunk in another language graded 2 is not flagged",
        RETRIEVAL_REVIEW,
        '        "CROSS-LANGUAGE-GRADE-2": any(item.grade == 2 for item in foreign),\n',
        '        "CROSS-LANGUAGE-GRADE-2": False,\n',
        f"{_RR}test_a_chunk_in_another_language_graded_2_is_flagged",
    ),
    _r210c(
        105,
        "a query revision needs no rationale",
        RETRIEVAL_REVIEW,
        '    if decision == "revise" and not _explained(rationale):\n',
        "    if False:\n",
        f"{_RR}test_a_malformed_review_is_refused[revise-no-rationale]",
    ),
    _r210c(
        106,
        "stopwords are not folded, so a Greek final sigma escapes them",
        RETRIEVAL_REVIEW,
        "word not in _FOLDED_STOPWORDS and",
        "word not in STOPWORDS and",
        f"{_RR}test_words_are_whole_case_folded_words_without_stopwords_or_numbers",
    ),
    _r210c(
        107,
        "write-form overwrites a review in progress",
        RETRIEVAL_REVIEW,
        "        if FORM_PATH.exists():\n",
        "        if False:\n",
        f"{_RR}test_write_form_never_overwrites_a_review",
    ),
    _r210c(
        108,
        "an accepted grade needs no rationale",
        RETRIEVAL_REVIEW,
        '        if decision == "accept" and not _explained(rationale):\n',
        "        if False:\n",
        f"{_RR}test_a_malformed_review_is_refused[accept-no-rationale]",
        f"{_RR}test_a_malformed_review_is_refused[accept-blank-rationale]",
        f"{_RR}test_every_judgment_decision_needs_a_rationale",
    ),
]


MUTATIONS: tuple[Mutation, ...] = (
    *STAGE_7_6,
    *STAGE_7_7,
    *STAGE_7_8,
    *STAGE_7_9,
    *STAGE_7_10,
    *STAGE_7_11,
    *STAGE_7_12,
    *STAGE_7_13,
    *STAGE_7_14,
    *STAGE_F2,
    *STAGE_F16,
    *STAGE_I4,
    *STAGE_F1,
    *STAGE_F2_ADR,
    *STAGE_M1,
    *STAGE_M2,
    *STAGE_F2_COMP,
    *STAGE_F3,
    *STAGE_F4,
    *STAGE_G1,
    *STAGE_H1,
    *STAGE_H2,
    *STAGE_H3,
    *STAGE_H4,
    *STAGE_H6,
    *STAGE_H7,
    *STAGE_H8,
    *STAGE_R2_2,
    *STAGE_R2_2B,
    *STAGE_R2_8AA,
    *STAGE_R2_8AC,
    *STAGE_R2_8AE,
    *STAGE_R2_8AG,
    *STAGE_R2_8AI,
    *STAGE_R2_8AK,
    *STAGE_R2_9B,
    *STAGE_R2_9K,
    *STAGE_R2_9X,
    *STAGE_R2_10A,
    *STAGE_R2_10B,
    *STAGE_R2_10C,
)
