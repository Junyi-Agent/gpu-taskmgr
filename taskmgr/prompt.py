"""[y/N] confirmations on stderr for `task host add` and `task host remove`."""

import os
import sys

from .textout import BOLD, YELLOW, paint


class Declined(Exception):
    """The user answered no."""


class NonInteractive(Exception):
    """A change needs a confirmation, but stdin is not a terminal and --yes was not given."""


class Ui:
    """Progress and prompts on stderr; answers from stdin."""

    def __init__(self, yes: bool) -> None:
        self.yes: bool = yes
        # TASKMGR_TEST_INTERACTIVE=1: test-only, answers come from a pipe instead of a TTY.
        self.interactive: bool = sys.stdin.isatty() or (
            os.environ.get("TASKMGR_TEST_INTERACTIVE") == "1"
        )

    @staticmethod
    def say(text: str = "", sgr: str | None = None) -> None:
        _ = sys.stderr.write(paint(text, sgr) + "\n")
        _ = sys.stderr.flush()

    def info(self, text: str, sgr: str | None = None) -> None:
        for line in text.splitlines() or [""]:
            self.say("      " + paint(line, sgr))

    def confirm(self, change: str) -> None:
        """Describe a change and ask; raises Declined or NonInteractive."""
        self.info("change: " + change, YELLOW)
        if self.yes:
            self.say("      %s y (--yes)" % paint("proceed? [y/N]", BOLD))
            return
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
