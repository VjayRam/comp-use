from comp_use.discovery.agent import RunTrace
from comp_use.schemas import ActionType, Artifact, Checkpoint, InputParam, OutputParam, Step


def _dedup_consecutive_steps(steps: list[Step]) -> list[Step]:
    # Harmless but wasteful duplicate consecutive steps (the same locator/action/value
    # decided twice in a row - observed live against MERIDIAN CORE's Funds Transfer
    # flow, see EXT_TASK_FIXES.md #8) collapse to one. Only exact-adjacent duplicates
    # are removed - a repeated action elsewhere in the flow that isn't consecutive is
    # left untouched, since that could be a genuinely distinct step.
    deduped: list[Step] = []
    for step in steps:
        if deduped and deduped[-1] == step:
            continue
        deduped.append(step)
    return deduped


def _drop_superseded_extracts(steps: list[Step]) -> list[Step]:
    """Keep only the LAST extract for any given extract_as.

    A model that doesn't get the locator right first time refines it and tries again,
    and every attempt was being recorded - live-observed as four extracts all named
    `shares_and_balances`, with progressively narrower selectors. They aren't caught by
    the adjacent-duplicate pass above because the locators differ.

    Dropping the earlier ones is output-preserving by construction: replay writes
    `outputs[extract_as]` per extract, so only the last one's value ever survived
    anyway. The earlier attempts were pure cost - and worse than cost, since each was
    one more locator that could time out and fail a run for a value nothing reads."""
    last_index_for: dict[str, int] = {}
    for i, step in enumerate(steps):
        if step.action == ActionType.EXTRACT and step.extract_as:
            last_index_for[step.extract_as] = i
    return [
        step
        for i, step in enumerate(steps)
        if not (step.action == ActionType.EXTRACT and step.extract_as)
        or last_index_for[step.extract_as] == i
    ]


def compile_artifact(
    trace: RunTrace,
    capability_name: str,
    target: dict,
    success_checkpoint: Checkpoint,
    output_schema: list[OutputParam],
) -> Artifact:
    input_schema: list[InputParam] = []
    seen = set()
    for step in trace.steps:
        vs = step.value_source
        if vs is not None and vs.param_name not in seen:
            seen.add(vs.param_name)
            input_schema.append(
                InputParam(
                    name=vs.param_name,
                    type=vs.param_type or "string",
                    required=True,
                    # The discovery-time literal, captured off to the side in
                    # trace.parameter_examples (never on the Step itself - see
                    # discovery/agent.py) - gives a reviewer/calling agent a concrete
                    # example per §3.2's "reviewable" requirement. None for
                    # credential-shaped params, which are deliberately never captured.
                    example=trace.parameter_examples.get(vs.param_name),
                )
            )

    output_schema = list(output_schema)
    seen_outputs = {o.name for o in output_schema}
    for step in trace.steps:
        if step.action == ActionType.EXTRACT and step.extract_as and step.extract_as not in seen_outputs:
            seen_outputs.add(step.extract_as)
            output_schema.append(OutputParam(name=step.extract_as, type="string"))
    # Declared by the agent itself, on the same extract decision that produced their
    # from_output (see discovery/agent.py) - a goal asking for a computed value (e.g.
    # a "total" summed from individual line items a page never totals itself) no
    # longer requires a human to hand-author the OutputParam(derive=...) after the
    # fact; discovery records the intent, replay performs the actual arithmetic.
    for derived in trace.derived_outputs:
        if derived.name not in seen_outputs:
            seen_outputs.add(derived.name)
            output_schema.append(derived)

    return Artifact(
        capability_name=capability_name,
        target=target,
        description=trace.goal,
        input_schema=input_schema,
        output_schema=output_schema,
        steps=_drop_superseded_extracts(_dedup_consecutive_steps(trace.steps)),
        success_checkpoint=success_checkpoint,
        # Deliberately left empty here for anything the target-level library already
        # covers: ReplayEngine resolves that library live, per target host, on every run
        # (see _resolve_outcome_patterns). Snapshotting it into each artifact instead
        # meant a fix to a shared pattern only reached capabilities recorded after it -
        # which is exactly how a wrong "SUPERVISOR OVERRIDE REQUIRED" anchor stayed
        # baked into every already-recorded capability. This field now holds only
        # patterns specific to THIS capability.
        outcome_patterns=[],
        created_from_run_id=trace.run_id,
    )
