"""Emit a no-credential variant of the task XML, purely to prove it registers.

The production XML uses LogonType=Password + RunLevel=HighestAvailable, which
requires elevation and a stored password. This variant swaps those for
InteractiveToken / LeastPrivilege so Task Scheduler will accept it without
credentials, which validates everything that actually matters about the file:
the Triggers, the Actions, the ExecutionTimeLimit and the RestartOnFailure block.
Delete the registered task afterwards.
"""

from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "nse_eod_daily.xml"
OUT = HERE / "_validate_tmp.xml"

text = SRC.read_text(encoding="utf-16")
text = text.replace("<LogonType>Password</LogonType>", "<LogonType>InteractiveToken</LogonType>")
text = text.replace(
    "<RunLevel>HighestAvailable</RunLevel>", "<RunLevel>LeastPrivilege</RunLevel>"
)
text = text.replace("<UserId>REPLACED_BY_SCHTASKS_RU</UserId>", "<UserId>NODE238\\HP</UserId>")
text = text.replace("NSE EOD Daily</URI>", "NSE EOD XMLVALIDATE</URI>")

OUT.write_bytes(b"\xff\xfe" + text.encode("utf-16-le"))
print(f"wrote {OUT}")
print("RestartOnFailure present:", "<RestartOnFailure>" in text)
print("Interval PT15M present  :", "<Interval>PT15M</Interval>" in text)
print("Count 3 present         :", "<Count>3</Count>" in text)
