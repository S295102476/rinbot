import importlib
from pathlib import Path
import random
import shutil
import sys
import types

import pytest

package = types.ModuleType("_minigame_idiom_tests")
package.__path__ = [str(Path(__file__).parents[1] / "plugins/minigames")]
sys.modules[package.__name__] = package
idioms = importlib.import_module(package.__name__ + ".idioms")


@pytest.mark.parametrize("value", ["春暖花开", "  春暖花开  ", "‘春暖花开’。", "（春暖花开）！", "\n春暖花开\t"])
def test_answer_only_strips_peripheral_whitespace_and_punctuation(value):
    assert idioms.normalise_answer(value) == "春暖花开"


@pytest.mark.parametrize("value", ["", "花开", "春暖花开了", "春 暖花开", "春暖，花开", "春暖花开🌸", "abcd", "1234", "春□花开", "#作答 春暖花开", None, 1234, " " * 257])
def test_answer_does_not_extract_or_repair_words(value):
    with pytest.raises(ValueError):
        idioms.normalise_answer(value)


def test_no_typo_or_traditional_conversion():
    assert idioms.normalise_answer("春暖花開") == "春暖花開"
    assert idioms.normalise_answer("春暧花开") == "春暧花开"
    bank = idioms.IdiomBank(["春暖花开"], rng=random.Random(2))
    assert "春暖花開" not in bank.next_question()["answers"]
    assert "春暧花开" not in bank.next_question()["answers"]


def test_frequency_sort_deduplicate_and_filter():
    bank = idioms.IdiomBank([("春暖花开", 4), ("春暖花开", 8), ("春暖花香", 7), ("一路顺风", 10), ("一路顺心", 20), ("ab12", 100), ("一路顺", 900)], pool_size=2, exclusions=["一路顺心"])
    assert bank.words == ("一路顺风", "春暖花开", "春暖花香")
    assert bank.pool == ("一路顺风", "春暖花开")
    assert bank.word_count == 3
    assert bank.pool_size == 2


def test_unique_masks_preferred():
    bank = idioms.IdiomBank({"春暖花开": 10, "春暖花香": 9}, rng=random.Random(3))
    for _ in range(20):
        assert len(bank.next_question()["answers"]) == 1


def test_multi_answer_index_includes_below_question_pool():
    bank = idioms.IdiomBank({"春暖花开": 10, "春暖花香": 1}, pool_size=1, rng=random.Random(3))
    other_masks = ["□□花开", "□暖□开", "□暖花□", "春□□开", "春□花□"]
    question = bank.next_question(excluded_masks=other_masks)
    assert question == {"mask": "春暖□□", "answers": ["春暖花开", "春暖花香"], "word": "春暖花开"}
    # Each returned question owns its answer snapshot.
    question["answers"].clear()
    assert len(bank.next_question(excluded_masks=other_masks)["answers"]) == 2


def test_previously_accepted_alternative_cannot_repeat():
    bank = idioms.IdiomBank(["春暖花开", "春暖花香"], pool_size=1)
    with pytest.raises(idioms.IdiomBankError):
        bank.next_question(excluded_words=["春暖花香"],
                           excluded_masks=["□□花开", "□暖□开", "□暖花□", "春□□开", "春□花□"])


def test_ten_questions_have_distinct_masks_and_all_answer_sets():
    bank = idioms.IdiomBank(["春暖花开", "一路顺风", "一心一意", "一马当先", "喜出望外", "欢天喜地", "千方百计", "兴高采烈", "无忧无虑", "风和日丽", "天高云淡"], rng=random.Random(17))
    questions = bank.questions(excluded_words=["天高云淡"])
    assert len(questions) == 10
    used_words, used_masks = set(), set()
    for question in questions:
        assert question["mask"].count("□") == 2
        assert len(question["mask"]) == 4
        assert question["word"] in question["answers"]
        assert not used_words.intersection(question["answers"])
        assert question["mask"] not in used_masks
        used_words.update(question["answers"])
        used_masks.add(question["mask"])
    assert "天高云淡" not in used_words


@pytest.mark.parametrize("count", [0, -1, True, 1.5, 11])
def test_invalid_or_too_many_questions_fail_without_partial_return(count):
    bank = idioms.IdiomBank(["春暖花开"])
    with pytest.raises(idioms.IdiomBankError):
        bank.questions(count)


@pytest.mark.parametrize("entries", [[], ["abc"], [("春暖花开", -1)], [("春暖花开", True)], [("春暖花开", "10")], [(1234, 0)]])
def test_invalid_banks_fail_closed(entries):
    with pytest.raises(idioms.IdiomBankError):
        idioms.IdiomBank(entries)


def test_load_real_frozen_bank_is_offline_and_reviewed():
    bank = idioms.load_default()
    assert bank.pool_size == 1000
    assert bank.word_count > 7000
    assert "春暖花开" in bank.pool
    assert "还本付息" not in bank.words
    assert "解放思想" not in bank.pool
    assert "精心设计" not in bank.pool
    assert all(len(word) == 4 for word in bank.words)
    assert len(bank.questions()) == 10
    assert idioms.IdiomBank.load_default().pool == bank.pool


def test_load_does_not_depend_on_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert idioms.load_default().pool_size == 1000


def test_missing_files_fail_closed(tmp_path):
    with pytest.raises(idioms.IdiomBankError) as failure:
        idioms.load_default(tmp_path)
    assert failure.value.code == "missing_file"
    assert failure.value.filename == "THUOCL_chengyu.txt"
    assert str(tmp_path.resolve()) in str(failure.value)


def test_cross_platform_line_endings_do_not_disable_valid_bank(tmp_path):
    source = Path(__file__).parents[1] / "data/minigames/idioms"
    target = tmp_path / "data/minigames/idioms"
    shutil.copytree(source, target)
    for filename in ("THUOCL_chengyu.txt", "LICENSE"):
        path = target / filename
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    assert idioms.load_default(tmp_path).pool_size == 1000


@pytest.mark.parametrize("filename", ["THUOCL_chengyu.txt", "LICENSE", "source.json", "exclusions.tsv"])
def test_corrupt_files_fail_closed(tmp_path, filename):
    source = Path(__file__).parents[1] / "data/minigames/idioms"
    target = tmp_path / "data/minigames/idioms"
    shutil.copytree(source, target)
    (target / filename).write_text("corrupt", encoding="utf-8")
    with pytest.raises(idioms.IdiomBankError) as failure:
        idioms.load_default(tmp_path)
    assert failure.value.filename == filename
    assert filename in str(failure.value)


def test_seeded_question_generation_is_reproducible():
    words = ["春暖花开", "一路顺风", "一心一意", "喜出望外"]
    first = idioms.IdiomBank(words, rng=random.Random(5)).questions(3)
    second = idioms.IdiomBank(words, rng=random.Random(5)).questions(3)
    assert first == second


def test_all_six_two_blank_positions_are_eligible():
    bank = idioms.IdiomBank(["春暖花开"], rng=random.Random(2))
    seen = set()
    for _ in range(6):
        question = bank.next_question(excluded_masks=seen)
        assert question["mask"].count("□") == 2
        seen.add(question["mask"])
    assert seen == {"□□花开", "□暖□开", "□暖花□", "春□□开", "春□花□", "春暖□□"}
    with pytest.raises(idioms.IdiomBankError):
        bank.next_question(excluded_masks=seen)


def test_production_two_blank_sessions_do_not_repeat_any_accepted_word():
    bank = idioms.load_default()
    for seed in range(20):
        bank._rng = random.Random(seed)
        questions = bank.questions(10)
        seen_words, seen_masks = set(), set()
        for question in questions:
            assert question["mask"].count("□") == 2
            assert not seen_words.intersection(question["answers"])
            assert question["mask"] not in seen_masks
            seen_words.update(question["answers"])
            seen_masks.add(question["mask"])


@pytest.mark.parametrize("filename", idioms.DATA_FILES)
def test_each_missing_file_is_named_precisely(tmp_path, filename):
    source = Path(__file__).parents[1] / "data/minigames/idioms"
    target = tmp_path / "data/minigames/idioms"
    shutil.copytree(source, target)
    (target / filename).unlink()
    with pytest.raises(idioms.IdiomBankError) as failure:
        idioms.load_default(tmp_path)
    assert failure.value.code == "missing_file"
    assert failure.value.filename == filename
    assert filename in str(failure.value)
    assert str(target) in str(failure.value)


@pytest.mark.parametrize("filename,content,code", [
    ("THUOCL_chengyu.txt", b'\xff\xfe', "encoding_error"),
    ("THUOCL_chengyu.txt", '春暖花开 wrong\n'.encode(), "format_error"),
    ("THUOCL_chengyu.txt", '春暖花开 123\n'.encode(), "checksum_error"),
    ("LICENSE", b'changed license', "checksum_error"),
    ("source.json", b'{bad json}', "format_error"),
    ("source.json", b'{}', "metadata_mismatch"),
    ("exclusions.tsv", '春暖花开 missing tab'.encode(), "format_error"),
])
def test_loading_error_categories_are_actionable(tmp_path, filename, content, code):
    source = Path(__file__).parents[1] / "data/minigames/idioms"
    target = tmp_path / "data/minigames/idioms"
    shutil.copytree(source, target)
    (target / filename).write_bytes(content)
    with pytest.raises(idioms.IdiomBankError) as failure:
        idioms.load_default(tmp_path)
    assert (failure.value.code, failure.value.filename) == (code, filename)


def test_permission_error_names_unreadable_file(monkeypatch):
    original = Path.read_bytes
    def blocked(path):
        if path.name == "LICENSE":
            raise PermissionError("private system details should not be copied")
        return original(path)
    monkeypatch.setattr(Path, "read_bytes", blocked)
    with pytest.raises(idioms.IdiomBankError) as failure:
        idioms.load_default()
    assert failure.value.code == "read_error"
    assert failure.value.filename == "LICENSE"
    assert "private system" not in str(failure.value)


def _diagnostic_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("_idiom_diagnostic_test",
        Path(__file__).parents[1] / "tools/check_idioms.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_diagnostic_is_offline_and_does_not_import_robot_or_read_config(tmp_path, monkeypatch, capsys):
    import builtins
    import socket
    source = Path(__file__).parents[1] / "data/minigames/idioms"
    shutil.copytree(source, tmp_path / "data/minigames/idioms")
    (tmp_path / "config.yaml").write_text("secret: DO_NOT_PRINT_ME", encoding="utf-8")
    tool = _diagnostic_module()
    original_import = builtins.__import__
    def guard(name, *args, **kwargs):
        if name.split(".")[0] in {"nonebot", "plugins", "httpx", "requests", "sqlalchemy"}:
            raise AssertionError("Unexpected external/service import")
        return original_import(name, *args, **kwargs)
    def no_network(*args, **kwargs):
        raise AssertionError("Network is forbidden for this tool")
    monkeypatch.setattr(builtins, "__import__", guard)
    monkeypatch.setattr(socket, "socket", no_network)
    assert tool.main(["--root", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    assert "1000 条" in output and "2 个空" in output
    assert str(tmp_path.resolve()) in output
    assert "DO_NOT_PRINT_ME" not in output


def test_diagnostic_lists_all_missing_files_and_returns_failure(tmp_path, capsys):
    assert _diagnostic_module().main(["--root", str(tmp_path)]) == 1
    output = capsys.readouterr().out
    for filename in idioms.DATA_FILES:
        assert filename in output
    assert "缺失文件" in output and "missing_file" in output


@pytest.mark.parametrize("interface", ["data_directory", "DATA_FILES", "load_default", "IdiomBankError"])
def test_diagnostic_reports_outdated_rules_interface_without_traceback(tmp_path, monkeypatch, capsys, interface):
    tool = _diagnostic_module()
    old = types.SimpleNamespace(data_directory=idioms.data_directory, DATA_FILES=idioms.DATA_FILES,
                                load_default=idioms.load_default, IdiomBankError=idioms.IdiomBankError)
    delattr(old, interface)
    monkeypatch.setattr(tool, "_load_module", lambda: old)
    assert tool.main(["--root", str(tmp_path)]) == 2
    output = capsys.readouterr()
    assert "版本不匹配" in output.out and interface in output.out
    assert "同步上传新版 plugins/minigames/idioms.py" in output.out
    assert "Traceback" not in output.out + output.err


def test_diagnostic_handles_legacy_exception_without_code_attribute(tmp_path, monkeypatch, capsys):
    tool = _diagnostic_module()
    class LegacyError(ValueError):
        pass
    def fail(_):
        raise LegacyError("old-format load failure")
    old = types.SimpleNamespace(data_directory=idioms.data_directory, DATA_FILES=idioms.DATA_FILES,
                                load_default=fail, IdiomBankError=LegacyError)
    monkeypatch.setattr(tool, "_load_module", lambda: old)
    assert tool.main(["--root", str(tmp_path)]) == 1
    output = capsys.readouterr()
    assert "load_error" in output.out
    assert "Traceback" not in output.out + output.err
