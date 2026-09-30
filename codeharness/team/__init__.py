"""Agent Team — persistent teammates, shared task board, structured protocol.

Package layout (single-direction dependencies):
  bus.py        MessageBus (.mailboxes/*.jsonl inboxes, Condition wakeup)
  protocol.py   ProtocolState / request_id lifecycle / plan gate constants
  worktree.py   git worktree create/resolve/remove (leader-only operations)
  teammate.py   TeammateState, handler factory, WORK/IDLE thread loop,
                TEAMMATE_TOOLS (built from the base tool registry, never
                from the leader's pool)
  manager.py    TeamManager registry + spawn/shutdown orchestration
  wakeup.py     leader wakeup thread (event-driven [Team events] delivery)
  tools.py      leader tool schemas + handlers (TEAM_TOOLS / TEAM_HANDLERS)
"""

from codeharness.team.bus import BUS, LEADER
from codeharness.team.protocol import ProtocolState
from codeharness.team.teammate import TEAMMATE_TOOLS, TeammateState
from codeharness.team.manager import TEAM, TeamManager
from codeharness.team.tools import TEAM_TOOLS, TEAM_HANDLERS
from codeharness.team import wakeup

__all__ = [
	"BUS", "LEADER",
	"ProtocolState",
	"TEAMMATE_TOOLS", "TeammateState",
	"TEAM", "TeamManager",
	"TEAM_TOOLS", "TEAM_HANDLERS",
	"wakeup",
]
