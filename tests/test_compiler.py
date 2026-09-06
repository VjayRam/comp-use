from comp_use.discovery.agent import RunTrace
from comp_use.discovery.compiler import compile_artifact
from comp_use.schemas import (
    ActionType, Checkpoint, CheckpointType, DerivedOutputSpec, Locator, LocatorStrategy,
    OutputParam, RiskTier, Step, ValueSource,
)


def test_compile_artifact_derives_input_schema_from_value_sources():
    trace = RunTrace(
        run_id="run_abc",
        goal="Open sub-account for member 12345",
        steps=[
            Step(action=ActionType.NAVIGATE, target="/member/search", risk_tier=RiskTier.SAFE),
            Step(
                action=ActionType.TYPE_TEXT,
                locator=Locator(strategy=LocatorStrategy.ROLE, value={"role": "textbox", "name": "Member ID"}),
                value_source=ValueSource(type="goal_parameter", param_name="member_id", param_type="string"),
                risk_tier=RiskTier.SAFE,
            ),
            Step(
                action=ActionType.CLICK,
                locator=Locator(strategy=LocatorStrategy.ROLE, value={"role": "button", "name": "Search"}),
                risk_tier=RiskTier.SAFE,
            ),
        ],
        final_url="http://localhost:5000/member/12345",
        succeeded=True,
    )
    success_checkpoint = Checkpoint(
        type=CheckpointType.ELEMENT_VISIBLE,
        locator=Locator(strategy=LocatorStrategy.ROLE, value={"role": "heading", "name": "Member Detail"}),
    )

    artifact = compile_artifact(
        trace,
        capability_name="lookup_member",
        target={"app": "mock_bank", "base_url": "http://localhost:5000"},
        success_checkpoint=success_checkpoint,
        output_schema=[],
    )

    assert artifact.capability_name == "lookup_member"
    assert len(artifact.input_schema) == 1
    assert artifact.input_schema[0].name == "member_id"
    assert artifact.input_schema[0].type == "string"
    assert artifact.created_from_run_id == "run_abc"
    assert len(artifact.steps) == 3


def test_compile_artifact_derives_output_schema_from_extract_steps():
    trace = RunTrace(
        run_id="run_extract",
        goal="Open sub-account",
        steps=[
            Step(action=ActionType.NAVIGATE, target="/sub-account/new", risk_tier=RiskTier.SAFE),
            Step(
                action=ActionType.EXTRACT,
                locator=Locator(strategy=LocatorStrategy.ROLE, value={"role": "generic", "name": "confirmation-number"}),
                extract_as="confirmation_number",
                risk_tier=RiskTier.SAFE,
            ),
        ],
        final_url="http://localhost:5000/sub-account/confirmation",
        succeeded=True,
    )
    success_checkpoint = Checkpoint(
        type=CheckpointType.ELEMENT_VISIBLE,
        locator=Locator(strategy=LocatorStrategy.ROLE, value={"role": "heading", "name": "Confirmation"}),
    )

    artifact = compile_artifact(
        trace,
        capability_name="open_sub_account",
        target={"app": "mock_bank", "base_url": "http://localhost:5000"},
        success_checkpoint=success_checkpoint,
        output_schema=[],
    )

    assert len(artifact.output_schema) == 1
    assert artifact.output_schema[0].name == "confirmation_number"
    assert artifact.output_schema[0].type == "string"


def test_compile_artifact_merges_agent_declared_derived_outputs():
    trace = RunTrace(
        run_id="run_derive",
        goal="Read the shares and report the total balance",
        steps=[
            Step(action=ActionType.NAVIGATE, target="/member/12345", risk_tier=RiskTier.SAFE),
            Step(
                action=ActionType.EXTRACT,
                locator=Locator(strategy=LocatorStrategy.CSS, value={"css": "table.shares"}),
                extract_as="shares_table",
                risk_tier=RiskTier.SAFE,
            ),
        ],
        final_url="http://localhost:5000/member/12345",
        succeeded=True,
        derived_outputs=[
            OutputParam(
                name="total_balance", type="string",
                derive=DerivedOutputSpec(from_output="shares_table", op="sum_currency"),
            )
        ],
    )
    success_checkpoint = Checkpoint(
        type=CheckpointType.ELEMENT_VISIBLE,
        locator=Locator(strategy=LocatorStrategy.ROLE, value={"role": "heading", "name": "Member"}),
    )

    artifact = compile_artifact(
        trace,
        capability_name="check_balance",
        target={"app": "mock_bank", "base_url": "http://localhost:5000"},
        success_checkpoint=success_checkpoint,
        output_schema=[],
    )

    names = [o.name for o in artifact.output_schema]
    assert names == ["shares_table", "total_balance"]
    derived = next(o for o in artifact.output_schema if o.name == "total_balance")
    assert derived.derive.from_output == "shares_table"
    assert derived.derive.op == "sum_currency"


def test_only_the_last_extract_for_a_name_is_kept():
    # Live-observed: a model that doesn't get the locator right first time refines it
    # and retries, and every attempt was recorded - four extracts all named
    # `shares_and_balances` with progressively narrower selectors. Replay writes
    # outputs[extract_as] per extract, so only the last one's value ever survived
    # anyway; the earlier ones were pure cost, and each was one more locator that could
    # time out and fail a run for a value nothing reads.
    from comp_use.discovery.compiler import _drop_superseded_extracts

    def extract(css, name):
        return Step(
            action=ActionType.EXTRACT,
            locator=Locator(strategy=LocatorStrategy.CSS, value={"css": css}),
            extract_as=name,
        )

    click = Step(
        action=ActionType.CLICK,
        locator=Locator(strategy=LocatorStrategy.ROLE, value={"role": "button", "name": "Search"}),
    )
    steps = [
        click,
        extract("table", "shares"),
        extract("table[border='1']", "shares"),
        extract("table[border='1'][width='100%']", "shares"),
        extract("td.total", "total"),
    ]

    kept = _drop_superseded_extracts(steps)

    assert kept[0] is click, "non-extract steps are untouched"
    shares = [s for s in kept if s.extract_as == "shares"]
    assert len(shares) == 1
    assert shares[0].locator.value == {"css": "table[border='1'][width='100%']"}, "the last one wins"
    # A differently-named extract is a different output and must survive.
    assert [s.extract_as for s in kept] == ["shares", "total"] or [s.extract_as for s in kept if s.extract_as] == ["shares", "total"]
