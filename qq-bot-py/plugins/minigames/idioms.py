"""Local, deterministic-rules idiom fill-in bank. No network or model calls.

THUOCL's frozen source and MIT license live in data/minigames/idioms.
Questions are plain dictionaries so the caller can persist the exact question
and accepted answers before presenting it. This module owns no game state.
"""
from __future__ import annotations

import hashlib
from itertools import combinations
import json
from pathlib import Path
import random
import re
from collections.abc import Iterable, Mapping
import unicodedata


_FOUR_HAN = re.compile(r"[\u4e00-\u9fff]{4}\Z")
_SOURCE_SHA256 = "c339d5d6e37d4f8ecdcb82f2a02b7fdfc66796f0a5215155f2aff8a77e89a7eb"
_LICENSE_SHA256 = "db4b8cca414db4cf487b933d07a0514f77caf44f9f3d392f2bc9d7ba0d511c58"
_SOURCE_COMMIT = "a30ce79d895d01ab5132a5c74c29703ff7efb4cc"
DATA_FILES = ("THUOCL_chengyu.txt", "LICENSE", "source.json", "exclusions.tsv")
_BLANK_PAIRS = tuple(combinations(range(4), 2))


class IdiomBankError(ValueError):
    """The bank is unavailable, invalid, or has no eligible question left."""

    def __init__(self, message, *, code="invalid_bank", filename=None, directory=None):
        super().__init__(message)
        self.code, self.filename, self.directory = code, filename, directory


def data_directory(root: Path | None = None) -> Path:
    """Return the effective absolute data directory, without reading config."""
    project = Path(root) if root is not None else Path(__file__).resolve().parents[2]
    return (project / "data" / "minigames" / "idioms").resolve()


def _masks_for(word: str) -> tuple[str, ...]:
    return tuple("".join("□" if i in holes else char for i, char in enumerate(word))
                 for holes in _BLANK_PAIRS)


def normalise_answer(text: str) -> str:
    """Strip peripheral punctuation/space, never repair or translate words.

    Simplified spellings are matched literally against the bank by the caller.
    Unicode CJK membership cannot distinguish simplified from traditional
    characters; we intentionally do not invent a conversion/correction here.
    """
    if not isinstance(text, str) or len(text) > 256:
        raise ValueError("请填写完整的四字成语")
    start, end = 0, len(text)
    def peripheral(char: str) -> bool:
        return char.isspace() or unicodedata.category(char).startswith("P")
    while start < end and peripheral(text[start]):
        start += 1
    while end > start and peripheral(text[end - 1]):
        end -= 1
    answer = text[start:end]
    if not _FOUR_HAN.fullmatch(answer):
        raise ValueError("请填写完整的四字成语，不要在中间添加标点或空格")
    return answer


class IdiomBank:
    """Injectable bank; highest-DF pool asks, the entire clean lexicon accepts.

    ``entries`` may be ``{word: df}``, an iterable of words, or (word, df)
    pairs. Non-four-Han entries are ignored and duplicate words take max DF.
    ``rng`` can be random.Random(seed) in tests; production uses SystemRandom.
    Lower-frequency words are still valid alternatives for an asked mask.
    """

    def __init__(
        self,
        entries: Mapping[str, int] | Iterable[str | tuple[str, int]],
        *,
        pool_size: int = 1000,
        exclusions: Iterable[str] = (),
        rng: random.Random | None = None,
    ):
        if type(pool_size) is not int or not 1 <= pool_size <= 10000:
            raise IdiomBankError("成语出题池大小无效")
        denied = set(exclusions)
        frequencies: dict[str, int] = {}
        source = entries.items() if isinstance(entries, Mapping) else entries
        for entry in source:
            if isinstance(entry, str):
                word, frequency = entry, 0
            else:
                try:
                    word, frequency = entry
                except (TypeError, ValueError) as exc:
                    raise IdiomBankError("成语词频条目格式无效") from exc
            if not isinstance(word, str):
                raise IdiomBankError("成语词条必须是文字")
            word = word.strip()
            if not _FOUR_HAN.fullmatch(word) or word in denied:
                continue
            if type(frequency) is not int or frequency < 0:
                raise IdiomBankError("成语词频必须是非负整数")
            frequencies[word] = max(frequencies.get(word, 0), frequency)
        if not frequencies:
            raise IdiomBankError("成语题库中没有可用的四字词条")
        self.words = tuple(sorted(frequencies, key=lambda word: (-frequencies[word], word)))
        self.pool = self.words[:pool_size]
        self.word_count = len(self.words)
        self.pool_size = len(self.pool)
        self._rng = rng if rng is not None else random.SystemRandom()
        answers: dict[str, list[str]] = {}
        for word in self.words:
            for mask in _masks_for(word):
                answers.setdefault(mask, []).append(word)
        self._answers = {mask: tuple(words) for mask, words in answers.items()}
        # Keep all eligible masks per source word. Pick a word, then a mask,
        # instead of over-weighting words that happen to have more valid holes.
        self._masks = {word: _masks_for(word) for word in self.pool}

    def next_question(
        self,
        excluded_words: Iterable[str] = (),
        excluded_masks: Iterable[str] = (),
    ) -> dict:
        """Return a fresh {mask, answers, word}; prefer globally unique masks.

        A candidate is omitted if *any* of its accepted answers was used before,
        so alternate answers cannot silently repeat a preceding question.
        """
        denied_words, denied_masks = set(excluded_words), set(excluded_masks)
        unique: list[tuple[str, list[str]]] = []
        ambiguous: list[tuple[str, list[str]]] = []
        for word, masks in self._masks.items():
            if word in denied_words:
                continue
            word_unique, word_ambiguous = [], []
            for mask in masks:
                if mask in denied_masks or denied_words.intersection(self._answers[mask]):
                    continue
                target = word_unique if len(self._answers[mask]) == 1 else word_ambiguous
                target.append(mask)
            if word_unique:
                unique.append((word, word_unique))
            if word_ambiguous:
                ambiguous.append((word, word_ambiguous))
        eligible = unique or ambiguous
        if not eligible:
            raise IdiomBankError("本局可用的成语题目已用完")
        word, masks = self._rng.choice(eligible)
        mask = self._rng.choice(masks)
        return {"mask": mask, "answers": list(self._answers[mask]), "word": word}

    def questions(
        self,
        count: int = 10,
        excluded_words: Iterable[str] = (),
        excluded_masks: Iterable[str] = (),
    ) -> list[dict]:
        """Draw distinct questions atomically; never silently return fewer."""
        if type(count) is not int or not 1 <= count <= self.pool_size:
            raise IdiomBankError("成语题目数量无效或题库不足")
        words, masks = set(excluded_words), set(excluded_masks)
        result = []
        for _ in range(count):
            question = self.next_question(words, masks)
            result.append(question)
            words.update(question["answers"])
            masks.add(question["mask"])
        return result

    @classmethod
    def load_default(cls, root: Path | None = None) -> "IdiomBank":
        return load_default(root)


def load_default(root: Path | None = None) -> IdiomBank:
    """Load the bundled frozen lexicon; root is the project root, not CWD.

    This is a strict offline load: damaged/missing files fail closed and should
    disable only the idiom feature at the service boundary, never chess games.
    No deserialization of executable objects, remote fetch or silent fallback.
    """
    folder = data_directory(root)

    def failure(filename, reason, code):
        return IdiomBankError(f"成语题库{reason}：{filename}（目录：{folder}）",
                              code=code, filename=filename, directory=str(folder))

    def read(filename):
        try:
            return (folder / filename).read_bytes()
        except FileNotFoundError as exc:
            raise failure(filename, "缺少文件", "missing_file") from exc
        except OSError as exc:
            raise failure(filename, f"文件无法读取（{type(exc).__name__}）", "read_error") from exc

    def decode(raw, filename):
        try:
            return raw.decode("utf-8")
        except UnicodeError as exc:
            raise failure(filename, "文件不是有效 UTF-8", "encoding_error") from exc

    raw = read("THUOCL_chengyu.txt")
    license_bytes = read("LICENSE")
    metadata_text = decode(read("source.json"), "source.json")
    exclusions_text = decode(read("exclusions.tsv"), "exclusions.tsv")
    try:
        metadata = json.loads(metadata_text)
    except ValueError as exc:
        raise failure("source.json", "来源元数据 JSON 格式错误", "format_error") from exc
    if (not isinstance(metadata, dict) or metadata.get("commit") != _SOURCE_COMMIT
            or metadata.get("sha256") != _SOURCE_SHA256
            or metadata.get("license") != "MIT"
            or metadata.get("quiz_pool_size") != 1000):
        raise failure("source.json", "来源元数据与锁定版本不一致", "metadata_mismatch")
    entries = []
    for number, row in enumerate(decode(raw, "THUOCL_chengyu.txt").splitlines(), 1):
        columns = row.split()
        if len(columns) != 2 or not re.fullmatch(r"[0-9]{1,12}", columns[1]):
            raise failure("THUOCL_chengyu.txt", f"词频格式错误（第 {number} 行）", "format_error")
        entries.append((columns[0], int(columns[1])))
    # Canonical LF hashing tolerates Git/SFTP line-ending conversion only;
    # word, frequency or license changes still fail closed.
    if hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest() != _SOURCE_SHA256:
        raise failure("THUOCL_chengyu.txt", "SHA256 校验失败，与锁定版本不一致", "checksum_error")
    if hashlib.sha256(license_bytes.replace(b"\r\n", b"\n")).hexdigest() != _LICENSE_SHA256:
        raise failure("LICENSE", "SHA256 校验失败，与原始许可不一致", "checksum_error")
    exclusions = []
    for number, row in enumerate(exclusions_text.splitlines(), 1):
        if not row.strip() or row.lstrip().startswith("#"):
            continue
        word, separator, reason = row.partition("\t")
        if not separator or not reason.strip() or not _FOUR_HAN.fullmatch(word):
            raise failure("exclusions.tsv", f"排除名单格式错误（第 {number} 行）", "format_error")
        exclusions.append(word)
    try:
        bank = IdiomBank(entries, pool_size=1000, exclusions=exclusions)
    except IdiomBankError as exc:
        raise failure("exclusions.tsv", "筛选后没有可用四字词条", "insufficient_words") from exc
    if bank.pool_size < 1000:
        raise failure("exclusions.tsv", f"筛选后出题词不足 1000 条，实际 {bank.pool_size} 条", "insufficient_words")
    return bank
