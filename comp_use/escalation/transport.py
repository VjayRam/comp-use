from comp_use.schemas import InterventionRequest


class ControlTransport:
    def notify(self, request: InterventionRequest) -> None:
        raise NotImplementedError

    def wait_for_resume(self) -> None:
        raise NotImplementedError


class LocalSharedBrowserTransport(ControlTransport):
    def notify(self, request: InterventionRequest) -> None:
        print(f"[ESCALATION] {request.reason} (step {request.current_step}) — "
              f"take over the browser window, then type 'resume' here.")

    def wait_for_resume(self) -> None:
        while True:
            typed = input("> ").strip().lower()
            if typed == "resume":
                return
