# CompUse — demo runbook

Everything needed to bring the stack up and drive discovery, replay, and their edge
cases from the chat UI.

- **Target:** MERIDIAN CORE at `https://web-sample.interface-hiring.com`
- **Demo operators** (the site's own sign-on page lists these): `teller1 / password`,
  and `super1 / password` for supervisor-only functions.

---

## 1. Setup

Run each service in its **own terminal**, from the repo root, in this order.

### 1.1 Postgres (must be first)

```powershell
docker start comp-use-postgres
docker inspect -f "{{.State.Status}}" comp-use-postgres   # expect: running
```

Use `docker start`, never `docker run` — the existing container holds every artifact
and run record. The backend's connection pool does not recover cleanly from a
Postgres that was down when it booted, which is why this goes first.

### 1.2 Mock app (optional — only for local mock-bank workflows, not MERIDIAN)

```powershell
.venv\Scripts\python.exe run_mock_app_host.py
```

Binds `0.0.0.0:5000` so the sandbox container reaches it at
`http://host.docker.internal:5000`.

### 1.3 Backend

```powershell
$env:COMP_USE_SANDBOX = "1"
.venv\Scripts\python.exe -m comp_use.cli serve --port 8126
```

`COMP_USE_SANDBOX` is **not** in `.env` and must be set on every start. Without it,
discovery drives Chromium on your own machine instead of an isolated Docker
container — no noVNC feed, and nothing to screen-record. `$env:` only applies to the
terminal you set it in, which is why the backend needs its own.

### 1.4 Frontend

```powershell
cd frontend
npm run dev
```

Serves on `http://localhost:5173`. It reaches the backend through
`frontend/.env.local` (`VITE_API_BASE=http://127.0.0.1:8126`).

### 1.5 Verify before recording

```powershell
docker inspect -f "{{.State.Status}}" comp-use-postgres
(Invoke-WebRequest http://127.0.0.1:8126/capabilities -UseBasicParsing).Content.Length
```

A length above ~1000 means the catalog loaded. **A length of `2` is an empty `[]`** —
the backend started before Postgres was ready. Kill it and repeat 1.3. A `200 OK` on
its own does not prove the catalog loaded.

### 1.6 Shutdown

```powershell
Get-CimInstance Win32_Process -Filter "Name like '%python%'" |
  Where-Object { $_.CommandLine -like "*comp_use.cli serve*" } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }

Get-CimInstance Win32_Process -Filter "Name like '%node%'" |
  Where-Object { $_.CommandLine -like "*vite*" } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }

docker stop comp-use-postgres
```

Then check nothing leaked: `docker ps --format "{{.Names}}"` should list no
`comp-use-sandbox-run-*` containers. One per run is created and removed
automatically; any left behind belong to a run whose process died.

---

## 2. Before you record

Set the target-site dropdown to `https://web-sample.interface-hiring.com`.

**Check the host's global fault-injection mode first.** MERIDIAN has a
`forcedInject` setting on its **System Settings** page that applies to *every*
authenticated page, not just one request. If a previous session left it on, your run
will hit that fault immediately after sign-on and discovery will refuse to record —
which looks like a broken capability but is the host's state.

Sign in as `teller1`, open **System Settings**, and confirm the forced-inject
selector reads *none* before starting. See §5 for using it deliberately.

---

## 3. Discovery prompts

Each is worded as a **re-record**, so it lands as a new draft version while the
approved version keeps serving calls. Drop the first line to record a brand-new
capability instead.

These goal texts are the artifacts' own recorded descriptions — the exact wording
that produced working recordings. They name a concrete literal *and* say it varies
per call. Paraphrasing them tends to make the model emit a parameter binding with no
value to type, which stalls the run.

### 3.1 `meridian_sign_on` (~6 steps)

```
Re-record the existing workflow meridian_sign_on.

Sign on to the MERIDIAN-style web application at https://web-sample.interface-hiring.com as operator teller1 with password 'password'. Choose 'MAIN-001 - Main Office' from the branch dropdown using a select_option action (operator_id, password and branch will each vary per call), then submit the sign-on form. Confirm you have arrived at the MAIN MENU, and extract the line that states which operator is signed on. Do not perform any other action.
```

### 3.2 `meridian_member_inquiry_by_number` (~9 steps)

```
Re-record the existing workflow meridian_member_inquiry_by_number.

Sign on to the MERIDIAN-style web application at https://web-sample.interface-hiring.com as operator teller1 with password 'password'. Choose 'MAIN-001 - Main Office' from the branch dropdown using a select_option action (operator_id, password and branch will each vary per call), then submit the sign-on form. Open 'Member Inquiry / Selection'. Search by member number for member 103001 (member_number will vary per call). Then extract the search RESULTS table listing the matched member - the table containing the member number and name, not the search form above it. Do not open the member's record.
```

### 3.3 `meridian_member_inquiry_by_name` (~10 steps — exercises a second dropdown)

```
Re-record the existing workflow meridian_member_inquiry_by_name.

Sign on to the MERIDIAN-style web application at https://web-sample.interface-hiring.com as operator teller1 with password 'password'. Choose 'MAIN-001 - Main Office' from the branch dropdown using a select_option action (operator_id, password and branch will each vary per call), then submit the sign-on form. Open 'Member Inquiry / Selection'. Use a select_option action to change the 'Search by' dropdown to Last Name, then search for last name 'Vaughan' (last_name will vary per call). Then extract the search RESULTS table listing the matched member(s) - the table containing member numbers and names, not the search form above it.
```

### 3.4 `meridian_member_balance` (~10 steps — the derived-output one)

Produces `total_balance` via `derive_op: sum_currency`, computed at replay by text
parsing and arithmetic, never by a model.

```
Re-record the existing workflow meridian_member_balance.

Sign on to the MERIDIAN-style web application at https://web-sample.interface-hiring.com as operator teller1 with password 'password'. Choose 'MAIN-001 - Main Office' from the branch dropdown using a select_option action (operator_id, password and branch will each vary per call), then submit the sign-on form. Open 'Member Inquiry / Selection', search by member number for member 103001 (member_number will vary per call), and open that member's record. Extract the SHARES / BALANCES table showing every share id, type, balance and status, as an output named shares_and_balances. Then, using derive_as/derive_op, also report the sum of all the share balances as an output named total_balance.
```

### 3.5 `meridian_update_member_info` (~15 steps)

```
Re-record the existing workflow meridian_update_member_info.

Sign on to the MERIDIAN-style web application at https://web-sample.interface-hiring.com as operator teller1 with password 'password'. Choose 'MAIN-001 - Main Office' from the branch dropdown using a select_option action (operator_id, password and branch will each vary per call), then submit the sign-on form. Open 'Member Inquiry / Selection', search by member number for member 102777 (member_number will vary per call), open that member's record, and choose 'Update Member Information'. Set the e-mail to 'katherine.johnson@example.com', the phone to '555-0187' and the mailing address to '14 Cornerstone Ave, Springfield' (email, phone and address will each vary per call). Save the changes, then extract the confirmation message or the updated contact details shown afterwards.
```

### 3.6 `meridian_open_new_share` (~16 steps — commits a write)

```
Re-record the existing workflow meridian_open_new_share.

Sign on to the MERIDIAN-style web application at https://web-sample.interface-hiring.com as operator teller1 with password 'password'. Choose 'MAIN-001 - Main Office' from the branch dropdown using a select_option action (operator_id, password and branch will each vary per call), then submit the sign-on form. Open 'Member Inquiry / Selection', search by member number for member 102777 (member_number will vary per call), open that member's record, and choose 'Open New Share'. Use a select_option action to choose the Regular Shares share type (share_type will vary per call) and enter an initial deposit of 5.00 (initial_deposit will vary per call). Continue to the review screen and then post it to actually open the share. Finally extract the confirmation number and the new share id from the resulting screen.
```

### 3.7 `meridian_place_account_hold` (~16 steps — supervisor-only, signs on as `super1`)

```
Re-record the existing workflow meridian_place_account_hold.

Sign on to the MERIDIAN-style web application at https://web-sample.interface-hiring.com as operator super1 with password 'password' - this is a supervisor-only function, so a teller cannot complete it. Choose 'MAIN-001 - Main Office' from the branch dropdown using a select_option action (operator_id, password and branch will each vary per call), then submit the sign-on form. Open 'Member Inquiry / Selection', search by member number for member 102777 (member_number will vary per call), open that member's record, and choose 'Place Account Hold'. Use select_option actions to choose share '102777-MMKT-13' (share_id will vary per call) and reason code FRAUD (reason_code will vary per call), and enter notes (notes will vary per call). Continue to the review screen, then post the hold to actually place it. Finally extract the confirmation number shown afterwards.
```

### 3.8 `meridian_transfer_funds` (~17 steps — longest, commits a write)

```
Re-record the existing workflow meridian_transfer_funds.

Sign on to the MERIDIAN-style web application at https://web-sample.interface-hiring.com as operator teller1 with password 'password'. Choose 'MAIN-001 - Main Office' from the branch dropdown using a select_option action (operator_id, password and branch will each vary per call), then submit the sign-on form. Open 'Member Inquiry / Selection', search by member number for member 103001 (member_number will vary per call), open that member's record, and choose 'Funds Transfer'. Select source share '103001-MMKT-11' and destination share '103001-MMKT-10' (source_share_id and destination_share_id will vary per call), enter an amount of 1.00 (amount will vary per call, always well within the source share's balance) and a memo (memo will vary per call). Click Continue to reach the confirmation screen, then click 'Post Transfer' to actually commit the transfer. Finally extract the confirmation number shown on the TRANSFER POSTED screen.
```

**Timing.** Median discovery is ~100 s, mean ~117 s. Budget roughly two minutes each;
`transfer_funds` and `place_account_hold` run longer. All eight is roughly twenty
minutes and ~120 LLM calls.

---

## 4. Replay prompts

Replay never calls an LLM — it re-executes the recorded artifact — so these finish in
5–10 seconds.

```
Run meridian_sign_on.
```

```
Run meridian_member_inquiry_by_number for member 103001.
```

```
Run meridian_member_inquiry_by_name for last name Vaughan.
```

```
Run meridian_member_balance for member 103001.
```

```
Run meridian_update_member_info for member 102777 with email katherine.johnson@example.com, phone 555-0187 and address "14 Cornerstone Ave, Springfield".
```

```
Run meridian_open_new_share for member 102777, share type "S0001 - Regular Shares", initial deposit 5.00.
```

```
Run meridian_place_account_hold for member 102777 on share "102777-MMKT-13 - Money Market" with reason "FRAUD - Suspected fraud" and notes "demo hold".
```

```
Run meridian_transfer_funds for member 103001, from share 103001-MMKT-11 to share 103001-MMKT-10, amount 1.00, memo "demo transfer".
```

Every capability also takes `operator_id`, `password` and `branch`; the chat agent
fills them from the artifact's recorded examples unless you name different ones.

---

## 5. Replay edge cases

These demonstrate the outcome taxonomy: a **business outcome** is the host correctly
saying no, a **recoverable condition** is worth retrying, and a **hard failure** is
neither. All three are reported, not crashed on.

### 5.1 Member not found — `business_outcome`

```
Run meridian_member_balance for member 999999.
```

Expect `RECORD NOT FOUND`. The run reports a business outcome with the host's own
wording, not a locator timeout.

### 5.2 Permission denied — `business_outcome`

Place Account Hold is supervisor-only. Force it as a teller:

```
Run meridian_place_account_hold for member 102777 on share "102777-MMKT-13 - Money Market" with reason "FRAUD - Suspected fraud" and notes "demo hold", signing on as operator teller1 instead of super1.
```

Expect *"Operator profile teller1 is not authorized to perform this function."*
Note the pattern is anchored on that sentence, **not** on the "SUPERVISOR OVERRIDE
REQUIRED" banner — the legitimate hold form carries the same banner as a warning
label, so anchoring there matched every successful run too.

### 5.3 Insufficient funds — `business_outcome`

```
Run meridian_transfer_funds for member 103001, from share 103001-MMKT-11 to share 103001-MMKT-10, amount 999999.00, memo "overdraft test".
```

Expect *"The transaction could not be validated"* plus the specific failing rule read
live off the page via the pattern's `detail_locator` — the difference between "your
request was rejected" and "insufficient available balance in the source share."

### 5.4 Field validation — `business_outcome`

```
Run meridian_update_member_info for member 102777 with email "not-an-email", phone 555-0187 and address "14 Cornerstone Ave, Springfield".
```

Expect *"Please correct the following"* and the specific rule
(*"E-mail address is not in a valid format"*). This is the natural per-field
validation page, distinct from the injected `validation` fault in §5.5.

### 5.5 Injected faults — the host's own taxonomy

Set these **globally** on MERIDIAN's **System Settings** page (`forcedInject`), signed
in as `teller1`. The `?inject=` query parameter is scoped to specific transactional
routes and will not fire on sign-on.

| Mode | Page you should see | Classified as |
|---|---|---|
| `notfound` | RECORD NOT FOUND | `business_outcome` |
| `permission` | SUPERVISOR OVERRIDE REQUIRED | `business_outcome` |
| `validation` | TRANSACTION REJECTED | `business_outcome` |
| `maintenance` | SCHEDULED MAINTENANCE IN PROGRESS, with a "Continue" link | `recoverable` — auto-recovers by clicking Continue, up to 3 retries |
| `server` | APPLICATION ERROR with an `ERR-…` reference | `hard_failure` |
| `timeout` | YOUR SESSION HAS TIMED OUT | `recoverable`, reported without an automatic recovery |

With a mode set, run any replay from §4 and watch the outcome panel.

**`maintenance` is the best one to record** — it is the only mode where the agent
visibly recovers on its own and the run still succeeds.

> **Do not set `timeout`.** It is a one-way trap in MERIDIAN's own tooling: once set
> globally, every authenticated page — including System Settings itself, and even a
> brand-new successful sign-on — reports "session timed out." There is no path back
> through the UI, and the shared demo host stays that way until interface.ai
> redeploys it. Session-expiry recovery needs re-authentication mid-flow, which
> `OutcomePattern.recovery_action` (a single UI step) cannot express.

**Always set the mode back to *none* when finished.** A forgotten setting is
indistinguishable from a broken capability on the next run.

---

## 6. Discovery edge cases

### 6.1 Discovery refuses to record a fault

Set `forcedInject: notfound` in System Settings, then start any discovery from §3.

The agent signs on successfully, meets the fault, and **stops before recording that
step** — an escalation asks a human to resolve it live. Without this, whatever the
model did after the error page would be baked in as canonical steps; an early
recording contained a failed sign-on *and* its retry that way.

### 6.2 Re-recording produces a draft, not an overwrite

Run any §3 prompt for a capability that already exists. The chat agent replies that
it will create a **new draft version** while the approved version keeps serving. The
catalog decides this, not the model's own flag — proposing a name that collides is a
re-record whether or not it says so.

Then, on the dashboard, approve or reject the draft.

### 6.3 Under-parameterization, and the hint that fixes it

Record with the parameter left implicit:

```
Record a new workflow called meridian_balance_unhinted.

Sign on at https://web-sample.interface-hiring.com as teller1 with password 'password' and branch 'MAIN-001 - Main Office', look up member 103001, and extract the SHARES / BALANCES table.
```

The member number may be recorded as a literal, producing a capability that only ever
works for 103001. Compare with §3.4, which names the parameters explicitly.

### 6.4 Human takeover mid-recording

Start any §3 discovery, and while it runs click **Take control** on the run panel.
The agent pauses at its next step boundary, hands you the live browser through the
noVNC frame, and **Hand back to agent** resumes the same session — not a fresh one.

There is a real, occasionally multi-second gap between clicking and the status
flipping, because a slow in-flight action has to finish first. The panel says so
while you wait.

### 6.5 Stopping a run

Start any §3 discovery and click **Stop run**. The run ends at its next step boundary
and its sandbox container is shut down with it. Confirm with:

```powershell
docker ps --format "{{.Names}}" | Select-String "comp-use-sandbox"
```

The run lands as `interrupted` — deliberately not `error`, because nothing failed.

### 6.6 Loop detection

If a run repeats the same action on the same control three times without the page
changing, it escalates rather than burning its remaining steps. Most easily seen by
taking control and navigating the browser somewhere the recorded flow does not
expect.

---

## 7. Troubleshooting

| Symptom | Cause |
|---|---|
| Every page shows RECORD NOT FOUND right after a successful sign-on | Global `forcedInject` left set. Clear it in System Settings. |
| Discovery drives your local Chromium; no noVNC feed | `COMP_USE_SANDBOX` not set in the backend's terminal. |
| `/capabilities` returns `[]` | Backend started before Postgres was ready. Restart the backend. |
| `skipped_decision` with `429 ... openrouter.ai` | NVIDIA failed first and the OpenRouter free tier (50/day, account-wide) caught the retry. Not a prompt problem. |
| `rate limit: waited Ns` in the backend log | The client-side limiter pacing NVIDIA to 40 requests/minute. Working as intended. |
| Discovery types nothing into a field, then loops | The model emitted a parameter binding with no literal to type. Restate the goal with the concrete example value *and* the parameter name. |
| A run is stuck `escalated` and nobody will resume it | `DELETE /runs/{id}?force=true`, or **Stop run** in the UI. |
