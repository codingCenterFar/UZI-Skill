"""Paper trading scaffold for UZI-Skill.

This package provides a simulation-first workflow:
- run analysis
- derive action using policy
- record virtual orders/fills/positions
- emit human-facing notifications
"""

from .config import PaperTradeConfig, load_config
from .run_cycle import run_cycle

__all__ = ["PaperTradeConfig", "load_config", "run_cycle"]
