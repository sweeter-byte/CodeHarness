import json
import os
from dotenv import load_dotenv
from openai import OpenAI
from tools import TOOLS, TOOL_HANDLERS
import hooks
from hooks import SESSION_STATS, trigger_hooks

load_dotenv(override=True)

MAX_CONSECUTIVE_REJECTIONS = 3


class Agent:
    def __init__(self):
        self.client = OpenAI(
            api_key=os.environ["DEEPSEEK_API_KEY"],
            base_url=os.environ["DEEPSEEK_BASE_URL"],
        )
        self.model = os.environ["DEEPSEEK_MODEL_ID"]
        self.system = f"You are a coding agent at {os.getcwd()}. Use tools to solve tasks. Act, don't explain."

    def _accumulate_tokens(self, response):
        """Add token usage from an LLM response to SESSION_STATS."""
        if response.usage:
            SESSION_STATS["prompt_tokens"] += response.usage.prompt_tokens
            SESSION_STATS["completion_tokens"] += response.usage.completion_tokens
            SESSION_STATS["total_tokens"] += response.usage.total_tokens

    def _call_llm(self, messages: list):
        """Single LLM call; accumulates tokens automatically."""
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": self.system}] + messages,
            tools=TOOLS,
            max_tokens=8000,
        )
        self._accumulate_tokens(response)
        return response

    def _execute_tool(self, handler, tool_call_id, tool_name, args, messages):
        """
        Run PreToolUse hooks → execute (or skip) → run PostToolUse hooks.
        Returns True if the tool actually executed, False if blocked/rejected.
        """
        # Reset the "ask user" flag before each tool call.
        hooks.PENDING_USER_ASK = None

        # ── PreToolUse ──
        hook_result = trigger_hooks("PreToolUse", tool_name, args)

        if hook_result is not None:
            # A hook returned non-None → execution blocked.
            output = hook_result
            print(f"\033[31m✗ BLOCKED {tool_name}: {output}\033[0m")
            executed = False
        elif hooks.PENDING_USER_ASK is not None:
            # Permission hook flagged "ask" → prompt the user.
            reason = hooks.PENDING_USER_ASK
            print(f"\033[33m  ⚠ {reason}\033[0m")
            answer = input("\033[33m  Allow? [y/N]: \033[0m").strip().lower()
            if answer != "y":
                output = (
                    "Error: User rejected this operation. "
                    "Do NOT retry via alternative commands or paths. "
                    "If you cannot complete the task without this operation, "
                    "report what you have done and stop."
                )
                print("\033[31m  ✗ Rejected by user\033[0m")
                executed = False
            else:
                output = handler(**args)
                executed = True
        else:
            # All hooks passed.
            output = handler(**args)
            executed = True

        # ── PostToolUse ──
        if executed:
            SESSION_STATS["tool_calls"] += 1
            trigger_hooks("PostToolUse", tool_name, args, output)

        messages.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": output,
        })
        return executed

    def agent_loop(self, messages: list):
        consecutive_rejections = 0

        while True:
            # If user rejected too many times, force-stop the loop.
            if consecutive_rejections >= MAX_CONSECUTIVE_REJECTIONS:
                messages.append({
                    "role": "user",
                    "content": f"The user has rejected {consecutive_rejections} consecutive operations. "
                               "Do NOT retry. Report what you have accomplished so far and stop.",
                })
                print(f"\033[31m✗ {consecutive_rejections} consecutive rejections, stopping agent loop.\033[0m")
                consecutive_rejections = 0
                response = self._call_llm(messages)
                msg = response.choices[0].message
                messages.append(msg.model_dump())
                return

            response = self._call_llm(messages)
            msg = response.choices[0].message
            messages.append(msg.model_dump())

            if not msg.tool_calls:
                return

            for tc in msg.tool_calls:
                args = json.loads(tc.function.arguments)
                handler = TOOL_HANDLERS.get(tc.function.name)

                if handler is None:
                    output = f"Error: Unknown tool '{tc.function.name}'"
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": output,
                    })
                    continue

                executed = self._execute_tool(handler, tc.id, tc.function.name, args, messages)
                if not executed:
                    consecutive_rejections += 1
                else:
                    consecutive_rejections = 0


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

        trigger_hooks("UserPromptSubmit", query)
        history.append({"role": "user", "content": query})
        agent.agent_loop(history)

        last = history[-1]
        if last.get("content"):
            print(last["content"])
        print()

    # Session ended 
    trigger_hooks("Stop", SESSION_STATS)
