import json
import os
import subprocess
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv(override=True)


class Agent:
    def __init__(self):
        self.client = OpenAI(
            api_key=os.environ["DEEPSEEK_API_KEY"],
            base_url=os.environ["DEEPSEEK_BASE_URL"],
        )
        self.model = os.environ["DEEPSEEK_MODEL_ID"]
        self.system = f"You are a coding agent at {os.getcwd()}. Use bash to solve tasks. Act, don't explain."
        self.tools = [
            {
                "type": "function",
                "function": {
                    "name": "bash",
                    "description": "Run a shell command.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "command": {"type": "string"},
                        },
                        "required": ["command"],
                    },
                },
            }
        ]

    def run_bash(self, command: str) -> str:
        dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
        if any(d in command for d in dangerous):
            return "Error: Dangerous command blocked"
        try:
            r = subprocess.run(
                command, shell=True, cwd=os.getcwd(),
                capture_output=True, text=True, errors="replace", timeout=120,
            )
            out = (r.stdout + r.stderr).strip()
            return out[:50000] if out else "(no output)"
        except subprocess.TimeoutExpired:
            return "Error: Timeout (120s)"
        except (FileNotFoundError, OSError) as e:
            return f"Error: {e}"

    def agent_loop(self, messages: list):
        while True:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "system", "content": self.system}] + messages,
                tools=self.tools,
                max_tokens=8000,
            )

            msg = response.choices[0].message
            # Append assistant message to history
            messages.append(msg.model_dump())

            # If no tool calls, the loop ends
            if not msg.tool_calls:
                return

            # Execute each tool call, collect results
            for tc in msg.tool_calls:
                command = json.loads(tc.function.arguments)["command"]
                print(f"\033[33m$ {command}\033[0m")
                output = self.run_bash(command)
                print(output[:200])
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

        # Print the model's final text response
        last = history[-1]
        if last.get("content"):
            print(last["content"])
        print()
