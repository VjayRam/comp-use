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
            typed = input("> ").strip().lower()
            if typed == "resume":
                break
        note = input("Briefly, what did you do? (one line, optional): ").strip()
        return note
