from mendarr.worker.check import pick_impersonation


def test_exact_match():
    assert pick_impersonation("chrome131", ["chrome131", "firefox135"]) == "chrome131"


def test_alias_picks_highest_firefox():
    avail = ["chrome131", "firefox133", "firefox135", "safari180"]
    assert pick_impersonation("firefox", avail) == "firefox135"


def test_alias_numeric_not_lexical():
    assert pick_impersonation("firefox", ["firefox99", "firefox135"]) == "firefox135"


def test_none():
    assert pick_impersonation("firefox", ["chrome131"]) is None
    assert pick_impersonation("opera", ["firefox135"]) is None
