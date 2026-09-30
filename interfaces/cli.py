"""Terminal interface for the CodeHarness backend."""


class CLI:
    def __init__(self, harness, input_fn=input, output_fn=print):
        self.harness = harness
        self.input = input_fn
        self.output = output_fn
        self.harness.set_approval_handler(self.request_approval)
        self.harness.set_status_handler(self.show_status)

    def request_approval(self, reason: str) -> bool:
        self.output(reason)
        answer = self.input("\033[33m  Allow? [y/N]: \033[0m")
        return answer.strip().lower() == "y"

    def show_status(self, message: str) -> None:
        self.output(message)

    def run(self) -> None:
        self.output(
            "Agent Loop (type q to quit, /context /compact /clear for context mgmt)\n"
        )
        while True:
            try:
                query = self.input("\033[36m>> \033[0m")
            except (EOFError, KeyboardInterrupt):
                break

            command = query.strip().lower()
            if command in ("q", "exit", ""):
                break
            if command.startswith("/"):
                self._run_slash_command(command)
                continue

            result = self.harness.run(query)
            if result:
                self.output(result)
            self.output("")

    def _run_slash_command(self, command: str) -> None:
        if command == "/context":
            result = self.harness.context_info()
        elif command == "/compact":
            result = self.harness.compact()
        elif command == "/clear":
            result = self.harness.clear()
        else:
            result = f"Unknown command: {command}\nAvailable: /context, /compact, /clear"
        self.output(result)
