"""Skills 的公开发现与提示词接口测试；所有文件均由 tmp_path 创建。"""

from pathlib import Path
from typing import cast

import pytest

from omt.core.skills import (
    SkillDefinition,
    discover_skills,
    render_skills_prompt,
)
from omt.core.tools.read import read


def _write_skill(root: Path, directory: str, content: str) -> Path:
    path = root / directory / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_text(content, encoding="utf-8")
    return path


def _skill_content(name: str, description: str = "审查代码变更") -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n正文内容\n"


def test_discover_skills_recurses_and_sorts_with_absolute_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "skills"
    zeta = _write_skill(root, "nested/zeta", _skill_content("zeta"))
    alpha = _write_skill(root, "alpha", _skill_content("alpha"))
    _ = (root / "ignored.md").write_text("不是 Skill", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = discover_skills([Path("skills")])

    assert result.skills == [
        SkillDefinition("alpha", "审查代码变更", alpha.resolve()),
        SkillDefinition("zeta", "审查代码变更", zeta.resolve()),
    ]
    assert result.diagnostics == []


def test_discover_skills_accepts_bom_crlf_multiline_yaml_and_body_delimiters(
    tmp_path: Path,
) -> None:
    file = _write_skill(tmp_path, "review", "")
    content = "\r\n".join(
        [
            "\ufeff---", "name: review", "description: |", "  审查代码。",
            "  适用于代码变更。", "metadata:", "  owner: example", "---",
            "正文", "---", "",
        ]
    )
    _ = file.write_bytes(content.encode("utf-8"))

    result = discover_skills([tmp_path])

    assert result.diagnostics == []
    assert result.skills == [
        SkillDefinition("review", "审查代码。\n适用于代码变更。", file.resolve())
    ]


@pytest.mark.parametrize(
    "content",
    [
        "",
        "name: review\ndescription: 示例\n",
        "---\nname: review\ndescription: 示例\n",
        "---\n\n---\n",
        "---\n- review\n---\n",
        "---\nname: [\n---\n",
        "---\ndescription: 示例\n---\n",
        "---\nname: review\n---\n",
        "---\nname: 123\ndescription: 示例\n---\n",
        "---\nname: review\ndescription: false\n---\n",
        "---\nname: ''\ndescription: 示例\n---\n",
        "---\nname: review\ndescription: '  '\n---\n",
        _skill_content("BadName"),
        _skill_content("-review"),
        _skill_content("review-"),
        _skill_content("bad--name"),
        _skill_content("bad_name"),
        _skill_content("a" * 65),
        _skill_content("review", "a" * 1025),
        "---\n!!python/object/apply:builtins.str [example]\n---\n",
    ],
)
def test_invalid_skills_are_diagnosed_without_blocking_valid_files(
    tmp_path: Path, content: str
) -> None:
    bad = _write_skill(tmp_path, "bad", content)
    good = _write_skill(tmp_path, "good", _skill_content("good"))

    result = discover_skills([tmp_path])

    assert result.skills == [
        SkillDefinition("good", "审查代码变更", good.resolve())
    ]
    assert len(result.diagnostics) == 1
    assert str(bad) in result.diagnostics[0]


def test_discover_skills_accepts_field_length_boundaries(tmp_path: Path) -> None:
    name = "a" * 64
    description = "字" * 1024
    file = _write_skill(tmp_path, "boundary", _skill_content(name, description))

    result = discover_skills([tmp_path])

    assert result.skills == [SkillDefinition(name, description, file.resolve())]
    assert result.diagnostics == []


def test_later_roots_override_duplicate_names_with_diagnostic(
    tmp_path: Path,
) -> None:
    user = tmp_path / "user"
    project = tmp_path / "project"
    original = _write_skill(user, "review", _skill_content("review", "用户版本"))
    override = _write_skill(project, "review", _skill_content("review", "项目版本"))

    result = discover_skills([user, project])

    assert result.skills == [
        SkillDefinition("review", "项目版本", override.resolve())
    ]
    assert len(result.diagnostics) == 1
    assert str(original) in result.diagnostics[0]
    assert str(override) in result.diagnostics[0]


def test_duplicate_names_within_root_use_sorted_path_order(tmp_path: Path) -> None:
    original = _write_skill(tmp_path, "a", _skill_content("review", "先发现"))
    override = _write_skill(tmp_path, "z", _skill_content("review", "后发现"))

    result = discover_skills([tmp_path])

    assert result.skills == [
        SkillDefinition("review", "后发现", override.resolve())
    ]
    assert len(result.diagnostics) == 1
    assert str(original) in result.diagnostics[0]


def test_overlapping_and_repeated_roots_do_not_report_self_conflicts(
    tmp_path: Path,
) -> None:
    root = tmp_path / "skills"
    file = _write_skill(root, "review", _skill_content("review"))

    result = discover_skills([tmp_path, root, root])

    assert result.skills == [
        SkillDefinition("review", "审查代码变更", file.resolve())
    ]
    assert result.diagnostics == []


def test_repeated_root_does_not_restore_an_overridden_version(tmp_path: Path) -> None:
    user = tmp_path / "user"
    project = tmp_path / "project"
    _ = _write_skill(user, "review", _skill_content("review", "用户版本"))
    override = _write_skill(project, "review", _skill_content("review", "项目版本"))

    result = discover_skills([user, project, user])

    assert result.skills == [
        SkillDefinition("review", "项目版本", override.resolve())
    ]
    assert len(result.diagnostics) == 1


def test_directory_named_skill_is_rejected_without_blocking_nested_skills(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "SKILL.md"
    file = _write_skill(directory, "review", _skill_content("review"))

    result = discover_skills([tmp_path])

    assert result.skills == [
        SkillDefinition("review", "审查代码变更", file.resolve())
    ]
    assert len(result.diagnostics) == 1
    assert str(directory) in result.diagnostics[0]


def test_discover_skills_expands_explicit_home_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / ".agents" / "skills"
    file = _write_skill(root, "review", _skill_content("review"))
    monkeypatch.setenv("HOME", str(tmp_path))

    result = discover_skills([Path("~/.agents/skills")])

    assert result.skills == [
        SkillDefinition("review", "审查代码变更", file.resolve())
    ]
    assert result.diagnostics == []


def test_discover_skills_trims_metadata_whitespace(tmp_path: Path) -> None:
    file = _write_skill(
        tmp_path, "review", "---\nname: ' review '\ndescription: ' 用途 '\n---\n"
    )

    result = discover_skills([tmp_path])

    assert result.skills == [SkillDefinition("review", "用途", file.resolve())]
    assert result.diagnostics == []


def test_empty_and_missing_roots_are_normal(tmp_path: Path) -> None:
    for roots in ([], [tmp_path], [tmp_path / "missing"]):
        result = discover_skills(roots)
        assert result.skills == []
        assert result.diagnostics == []


def test_file_root_is_reported_and_other_roots_are_scanned(tmp_path: Path) -> None:
    not_directory = tmp_path / "file"
    _ = not_directory.write_text("示例", encoding="utf-8")
    valid = tmp_path / "valid"
    _ = _write_skill(valid, "review", _skill_content("review"))

    result = discover_skills([not_directory, valid])

    assert [skill.name for skill in result.skills] == ["review"]
    assert len(result.diagnostics) == 1
    assert str(not_directory) in result.diagnostics[0]


def test_invalid_encoding_is_reported_without_file_contents(tmp_path: Path) -> None:
    file = _write_skill(tmp_path, "bad", "")
    _ = file.write_bytes(b"\xff\xfe")

    result = discover_skills([tmp_path])

    assert result.skills == []
    assert len(result.diagnostics) == 1
    assert str(file) in result.diagnostics[0]


def test_yaml_error_diagnostic_does_not_expose_content(tmp_path: Path) -> None:
    marker = "PRIVATE_TEST_CONTENT"
    file = _write_skill(
        tmp_path, "bad", f"---\nname: review\ndescription: [{marker}\n---\n"
    )

    result = discover_skills([tmp_path])

    assert result.skills == []
    assert str(file) in result.diagnostics[0]
    assert marker not in "\n".join(result.diagnostics)


def test_non_regular_skill_is_rejected_without_opening_fifo(
    tmp_path: Path, non_regular_file: Path
) -> None:
    file = non_regular_file.rename(tmp_path / "SKILL.md")

    result = discover_skills([tmp_path])

    assert result.skills == []
    assert len(result.diagnostics) == 1
    assert str(file) in result.diagnostics[0]


def test_read_error_does_not_block_other_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unreadable = _write_skill(tmp_path, "bad", _skill_content("bad"))
    _ = _write_skill(tmp_path, "good", _skill_content("good"))
    original = Path.read_text

    def read_text(
        path: Path, encoding: str | None = None, errors: str | None = None
    ) -> str:
        if path == unreadable:
            raise PermissionError("测试拒绝读取")
        return original(path, encoding=encoding, errors=errors)

    monkeypatch.setattr(Path, "read_text", read_text)

    result = discover_skills([tmp_path])

    assert [skill.name for skill in result.skills] == ["good"]
    assert len(result.diagnostics) == 1
    assert str(unreadable) in result.diagnostics[0]


def test_scan_error_does_not_block_later_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unreadable = tmp_path / "unreadable"
    unreadable.mkdir()
    valid = tmp_path / "valid"
    _ = _write_skill(valid, "review", _skill_content("review"))
    original = Path.rglob

    def rglob(path: Path, pattern: str) -> list[Path]:
        if path == unreadable:
            raise PermissionError("测试拒绝扫描")
        return list(original(path, pattern))

    monkeypatch.setattr(Path, "rglob", rglob)

    result = discover_skills([unreadable, valid])

    assert [skill.name for skill in result.skills] == ["review"]
    assert len(result.diagnostics) == 1
    assert str(unreadable) in result.diagnostics[0]


def test_discover_skills_does_not_scan_implicit_home_or_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = _write_skill(tmp_path / ".agents" / "skills", "review", _skill_content("review"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    result = discover_skills([])

    assert result.skills == []
    assert result.diagnostics == []


def test_discover_skills_rereads_changes_without_cache(tmp_path: Path) -> None:
    file = _write_skill(tmp_path, "review", _skill_content("review", "旧描述"))
    assert discover_skills([tmp_path]).skills[0].description == "旧描述"
    _ = file.write_text(_skill_content("review", "新描述"), encoding="utf-8")

    assert discover_skills([tmp_path]).skills[0].description == "新描述"


def test_render_skills_prompt_is_empty_without_skills() -> None:
    assert render_skills_prompt([]) == ""


def test_render_skills_prompt_contains_metadata_and_loading_rules_only(
    tmp_path: Path,
) -> None:
    file = _write_skill(tmp_path, "review", _skill_content("review"))
    result = discover_skills([tmp_path])
    # 渲染阶段只使用元数据，不再次读取正文。
    file.unlink()

    prompt = render_skills_prompt(result.skills)

    assert "### review" in prompt
    assert "用途：审查代码变更" in prompt
    assert str(file.resolve()) in prompt
    assert "read" in prompt
    assert "offset=1、limit=false" in prompt
    assert "SKILL.md 所在目录" in prompt
    assert "更高优先级指令" in prompt
    assert "正文内容" not in prompt


def test_render_skills_prompt_flattens_multiline_descriptions(tmp_path: Path) -> None:
    skill = SkillDefinition("review", "第一行\n  第二行\t补充", tmp_path / "SKILL.md")

    prompt = render_skills_prompt([skill])

    assert "- 用途：第一行 第二行 补充\n" in prompt


def test_discovered_location_can_load_full_skill_with_existing_read_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = _skill_content("review") + "步骤\n" * 60 + "末尾指引\n"
    file = _write_skill(tmp_path / "skills", "review", content)
    other_cwd = tmp_path / "other"
    other_cwd.mkdir()
    result = discover_skills([tmp_path / "skills"])
    monkeypatch.chdir(other_cwd)

    loaded = cast(
        object,
        read.invoke(
            {"file": str(result.skills[0].file_path), "offset": 1, "limit": False}
        ),
    )

    assert str(file.resolve()) in render_skills_prompt(result.skills)
    assert isinstance(loaded, str)
    assert loaded == content
    assert loaded.endswith("末尾指引\n")
