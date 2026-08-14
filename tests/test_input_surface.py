"""The API's input surface carries only what the submitter decides.

These fields used to sit on the shared base model, which is also the input model,
so a client could set its own skill to ``approved`` or write its own rating and
download count — the review gate was bypassable with one PUT.
"""
import pytest

from app.schemas.skill import SkillCreate, SkillRead, SkillSummary, SkillUpdate

DERIVED = ["rating", "downloads", "isTrending", "ratingDist", "reviewCount",
           "status", "version", "author", "uploaderEid", "uploaderName"]


@pytest.mark.parametrize("field", DERIVED)
def test_a_derived_field_cannot_be_set_on_create(field):
    model = SkillCreate.model_validate({"name": "T", field: "x" if field in
                                        ("downloads", "status", "version",
                                         "author", "uploaderEid", "uploaderName")
                                        else 9})
    assert field not in model.model_fields_set


@pytest.mark.parametrize("field", DERIVED)
def test_a_derived_field_cannot_be_set_on_update(field):
    model = SkillUpdate.model_validate({field: "x" if field in
                                        ("downloads", "status", "version",
                                         "author", "uploaderEid", "uploaderName")
                                        else 9})
    assert field not in model.model_fields_set


def test_a_client_cannot_approve_its_own_skill():
    """The one that matters: status is the field that decides public visibility."""
    assert "status" not in SkillUpdate.model_fields
    assert "status" not in SkillCreate.model_fields


def test_the_author_can_still_edit_what_is_theirs():
    m = SkillUpdate.model_validate({
        "name": "N", "description": "d", "tags": ["a"], "isFree": False,
        "examplePrompt": "p", "instructions": "---\nname: N\n---\n",
    })
    assert m.name == "N" and m.tags == ["a"] and m.is_free is False


def test_reads_still_carry_the_derived_fields():
    """Removing them from the input must not remove them from the output."""
    for field in ("rating", "downloads", "is_trending", "review_count", "status",
                  "version", "author", "uploader_eid"):
        assert field in SkillRead.model_fields


def test_the_list_says_why_a_skill_is_not_public_yet():
    """An admin looking at 'pending' has to be able to tell what it is waiting on."""
    assert "audit_state" in SkillSummary.model_fields
    assert "audit_state" in SkillRead.model_fields
