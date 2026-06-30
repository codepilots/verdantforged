import pytest
from skill_library.models import SkillCard, SkillFileRef

def test_skill_card_minimal():
    c = SkillCard(name="summarize", version="1.0.0",
                  description="One-paragraph summary")
    assert c.license == "Apache-2.0"
    assert c.summary == ""

def test_skill_card_rejects_bad_name():
    with pytest.raises(ValueError):
        SkillCard(name="Bad Name", version="1.0.0", description="x")

def test_skill_card_rejects_bad_version():
    with pytest.raises(ValueError):
        SkillCard(name="ok", version="latest", description="x")

def test_skill_file_ref():
    f = SkillFileRef(filename="SKILL.md", sha256="a"*64, size_bytes=12,
                     content_type="text/markdown")
    assert f.size_bytes == 12