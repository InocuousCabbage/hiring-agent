"""browser — shared Playwright primitives for the hiring-agent.

Re-exports only `session`, the single browser-open context manager every
Playwright caller in the codebase uses (JD scraper today; every future
apply adapter tomorrow).
"""

from .session import session

__all__ = ["session"]
