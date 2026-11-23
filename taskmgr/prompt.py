"""[y/N] confirmations on stderr for `task create host` and `task delete host`.

Two kinds of change: ordinary ones (--yes answers them) and destructive ones (deleting
something outside what the run installs). --yes never answers a destructive change: it
needs its own flag (allow_destructive, e.g. `--prune-old-code`) or a "y" typed in a
terminal. In plan mode nothing is asked and nothing is changed: every change is reported.
"""

import os
import sys

from . import textout
from .textout import BOLD, YELLOW, clean, paint


class Declined(Exception):
    """The user answered no."""


class NonInteractive(Exception):
    """A change needs a confirmation, but stdin is not a terminal and --yes was not given."""


class Planned(Exception):
    """Plan mode: the change was reported, not made."""


class Kept(Exception):
    """A destructive change was not made: no flag allows it and nobody can be asked."""


class Ui:
    """Progress and prompts on stderr; answers from stdin."""

    def __init__(self, yes: bool, plan: bool = False, allow_destructive: bool = False) -> None:
        self.yes: bool = yes
        self.plan: bool = plan
        self.allow_destructive: bool = allow_destructive
        # TASKMGR_TEST_INTERACTIVE=1: test-only, answers come from a pipe instead of a TTY.
        self.interactive: bool = sys.stdin.isatty() or (
            os.environ.get("TASKMGR_TEST_INTERACTIVE") == "1"
        )

    @staticmethod
    def say(text: str, sgr: str | None = None) -> None:
        textout.say(text, sgr)

    def info(self, text: str, sgr: str | None = None) -> None:
        """Indented lines of text (which may come from the server: cleaned)."""
        for line in text.splitlines() or [""]:
            self.say("      " + paint(clean(line), sgr))

    def confirm(self, change: str, destructive: bool = False) -> None:
        """Describe a change and ask; raises Declined or NonInteractive, Planned in plan
        mode, Kept for a destructive change that is not allowed and cannot be asked."""
        if self.plan:
            self.info("would change: " + change, YELLOW)
            raise Planned(change)
        self.info("change: " + change, YELLOW)
        if destructive and self.allow_destructive:
            self.say("      %s y (allowed by flag)" % paint("proceed? [y/N]", BOLD))
            return
        if destructive and not self.interactive:
            raise Kept(change)
        if self.yes and not destructive:
            self.say("      %s y (--yes)" % paint("proceed? [y/N]", BOLD))
            return
        if destructive and self.yes:
            self.info("this deletes data; --yes does not answer it", YELLOW)
        if not self.interactive:
            raise NonInteractive(
                "a change is needed but stdin is not a terminal; run it in a terminal or "
                + "pass --yes (nothing was changed by this step)"
            )
        _ = sys.stderr.write("      %s " % paint("proceed? [y/N]", BOLD))
        _ = sys.stderr.flush()
        answer = sys.stdin.readline()
        if not answer:
            self.say("")
        if answer.strip().lower() not in ("y", "yes"):
            raise Declined("declined: " + change)
