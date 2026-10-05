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
CURRENT = "`0017_llm_invocation_retention`"
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
)
