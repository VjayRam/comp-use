from comp_use.schemas import InterventionRequest


class ControlTransport:
    def notify(self, request: InterventionRequest) -> None:
        raise NotImplementedError

    def wait_for_resume(self) -> str:
        raise NotImplementedError


class LocalSharedBrowserTransport(ControlTransport):
    def notify(self, request: InterventionRequest) -> None:
        print(f"[ESCALATION] {request.reason} (step {request.current_step}) — "
              f"take over the browser window, then type 'resume' here.")

    def wait_for_resume(self) -> str:
        while True:
            try:
                typed = input("> ").strip().lower()
            except EOFError:
                raise RuntimeError(
                    "Escalation requires a human to type 'resume' in an interactive "
                    "terminal, but stdin is closed/non-interactive. Re-run in an "
                    "interactive shell, or avoid triggering escalation (e.g. pass "
                    "--confirm-risky, or use params/artifacts that don't hit a risky "
                    "step or a drift-diagnosis review)."
                ) from None
            if typed == "resume":
                break
        try:
            note = input("Briefly, what did you do? (one line, optional): ").strip()
        except EOFError:
            note = ""
        return note
