from services.memory import skill_extractor


def test_duplicate_title_skips_invalid_skill_rows():
    rows = [
        "bad-row",
        None,
        {"title": 123},
        {"title": "Small PR workflow"},
    ]

    assert skill_extractor._has_duplicate_title(rows, "small pr workflow")
    assert not skill_extractor._has_duplicate_title(rows, "release checklist")


def test_near_reworded_titles_count_as_duplicates():
    rows = [{"title": "Verify and Push Feature Slices"}]
    assert skill_extractor._has_duplicate_title(rows, "Verify and Push Completed Feature Slices")
    assert skill_extractor._has_duplicate_title(rows, "Verify and Push MVP Slices")
    assert not skill_extractor._has_duplicate_title(rows, "Create Liquibase Seed SQL")
    assert not skill_extractor._has_duplicate_title(rows, "Push Changes")
