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
  tools.py      leader tool schemas + runtime-bound handler factory
"""

from codeharness.team import wakeup
from codeharness.team.bus import BUS, LEADER
from codeharness.team.manager import TEAM, TeamManager
from codeharness.team.protocol import ProtocolState
from codeharness.team.teammate import TEAMMATE_TOOLS, TeammateState
from codeharness.team.tools import TEAM_TOOLS, make_team_handlers

__all__ = [
	"BUS",
	"LEADER",
	"TEAM",
	"TEAMMATE_TOOLS",
	"TEAM_TOOLS",
	"ProtocolState",
	"TeamManager",
	"TeammateState",
	"make_team_handlers",
	"wakeup",
]
