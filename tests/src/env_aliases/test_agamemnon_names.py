"""AGAMEMNON_* environment names configure the app the code still reads as ODYSSEUS_*.

The product is Agamemnon, but every variable was ODYSSEUS_*. A new operator
who wrote AGAMEMNON_DATA_DIR got the default data folder with no warning.
"""
from src import env_aliases


def test_an_agamemnon_name_sets_the_name_the_code_reads():
    env = {"AGAMEMNON_DATA_DIR": "/srv/agamemnon", "PATH": "/bin"}
    assert env_aliases.apply(env) == []
    assert env["ODYSSEUS_DATA_DIR"] == "/srv/agamemnon"
    assert env["PATH"] == "/bin"


def test_a_legacy_name_alone_keeps_working():
    env = {"ODYSSEUS_DATA_DIR": "/srv/odysseus"}
    env_aliases.apply(env)
    assert env == {"ODYSSEUS_DATA_DIR": "/srv/odysseus"}


def test_the_agamemnon_name_wins_a_conflict_and_says_so():
    env = {"AGAMEMNON_INPROCESS_TASKS": "0", "ODYSSEUS_INPROCESS_TASKS": "1",
           "AGAMEMNON_SCRIPT_HOST": "localhost", "ODYSSEUS_SCRIPT_HOST": "localhost"}
    assert env_aliases.apply(env) == ["ODYSSEUS_INPROCESS_TASKS"]
    assert env["ODYSSEUS_INPROCESS_TASKS"] == "0"
