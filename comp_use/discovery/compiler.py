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

    return Artifact(
        capability_name=capability_name,
        target=target,
        description=trace.goal,
        input_schema=input_schema,
        output_schema=output_schema,
        steps=_dedup_consecutive_steps(trace.steps),
        success_checkpoint=success_checkpoint,
        created_from_run_id=trace.run_id,
    )
