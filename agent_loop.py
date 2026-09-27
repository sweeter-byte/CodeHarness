import json
import os
from dotenv import load_dotenv
from openai import OpenAI
from tools import TOOLS, TOOL_HANDLERS
from permission import PermissionManager

load_dotenv(override=True)


class Agent:
    def __init__(self):
        self.client = OpenAI(
            api_key=os.environ["DEEPSEEK_API_KEY"],
            base_url=os.environ["DEEPSEEK_BASE_URL"],
        )
        self.model = os.environ["DEEPSEEK_MODEL_ID"]
        self.system = f"You are a coding agent at {os.getcwd()}. Use tools to solve tasks. Act, don't explain."
        self.perm = PermissionManager()

    def agent_loop(self, messages: list):
        while True:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "system", "content": self.system}] + messages,
                tools=TOOLS,
                max_tokens=8000,
            )

            msg = response.choices[0].message
            messages.append(msg.model_dump())

            if not msg.tool_calls:
                return

            for tc in msg.tool_calls:
                args = json.loads(tc.function.arguments)
                handler = TOOL_HANDLERS.get(tc.function.name)
                if handler is None:
                    output = f"Error: Unknown tool '{tc.function.name}'"
                else:
                    # Permission check before execution.
                    decision, reason = self.perm.check(tc.function.name, args)
                    if decision == "deny":
                        output = f"Error: Permission denied - {reason}"
                        print(f"\033[31m✗ DENIED {tc.function.name}({args}): {reason}\033[0m")
                    elif decision == "ask":
                        print(f"\033[33m> {tc.function.name}({args})\033[0m")
                        print(f"\033[33m  ⚠ {reason}\033[0m")
                        answer = input("\033[33m  Allow? [y/N]: \033[0m").strip().lower()
                        if answer != "y":
                            output = "Error: User rejected this operation"
                            print("\033[31m  ✗ Rejected by user\033[0m")
                        else:
                            output = handler(**args)
                            print(str(output)[:200])
                    else:
                        print(f"\033[33m> {tc.function.name}({args})\033[0m")
                        output = handler(**args)
                        print(str(output)[:200])
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": output,
                })


if __name__ == "__main__":
    agent = Agent()
    print("Agent Loop (type q to quit)\n")

    history = []
    while True:
        try:
            query = input("\033[36m>> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break

        history.append({"role": "user", "content": query})
        agent.agent_loop(history)

        last = history[-1]
        if last.get("content"):
            print(last["content"])
        print()
