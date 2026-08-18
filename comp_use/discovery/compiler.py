from comp_use.discovery.agent import RunTrace
from comp_use.schemas import Artifact, Checkpoint, InputParam, OutputParam


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
        if vs is not None and vs.type == "goal_parameter" and vs.param_name not in seen:
            seen.add(vs.param_name)
            input_schema.append(
                InputParam(name=vs.param_name, type=vs.param_type or "string", required=True)
            )

    return Artifact(
        capability_name=capability_name,
        target=target,
        description=trace.goal,
        input_schema=input_schema,
        output_schema=output_schema,
        steps=trace.steps,
        success_checkpoint=success_checkpoint,
        created_from_run_id=trace.run_id,
    )
