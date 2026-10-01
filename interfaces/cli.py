"""Terminal interface for the CodeHarness backend."""

import json


class CLI:
    def __init__(self, harness, input_fn=input, output_fn=print):
        self.harness = harness
        self.input = input_fn
        self.output = output_fn
        self.harness.set_approval_handler(self.request_approval)
        self.harness.set_status_handler(self.show_status)
        self.harness.set_async_result_handler(self.show_async_result)

    def request_approval(self, reason: str) -> bool:
        self.output(reason)
        answer = self.input("\033[33m  Allow? [y/N]: \033[0m")
        return answer.strip().lower() == "y"

    def show_status(self, message: str) -> None:
        self.output(message)

    def show_async_result(self, result: str) -> None:
        self.output(f"\n{result}\n")
        self.output("\033[36m>> \033[0m")

    def run(self) -> None:
        self.output(
            "Agent Loop (type q to quit, /context /compact /clear /goal for commands)\n"
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
        elif command.startswith("/goal"):
            result = self._handle_goal_command(command)
        else:
            result = (
                f"Unknown command: {command}\n"
                "Available: /context, /compact, /clear, /goal"
            )
        self.output(result)

    def _handle_goal_command(self, command: str) -> str:
        """Handle /goal subcommands: /goal, /goal <condition>, /goal clear."""
        gc = self.harness.goal_controller
        if gc is None:
            return "Goal Loop is not available."

        parts = command.split(maxsplit=1)
        arg = parts[1].strip() if len(parts) > 1 else ""

        if not arg:
            # /goal → show status
            status = gc.get_status()
            if not status.get("active"):
                return "No active goal. Use '/goal <condition>' to set one."
            return json.dumps(status, ensure_ascii=False, indent=2)

        if arg.lower() == "clear":
            return gc.clear_goal()

        # /goal <condition> → set goal
        return gc.set_goal(arg)
