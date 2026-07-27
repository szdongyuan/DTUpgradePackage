import builtins
import json
import math
import os

import pytest

from base import json_merge
from base.json_merge import merge_json_files
from base.json_merge import merge_json_files_with_reason


class EmptyPath:
    def __fspath__(self):
        return ""


class BytesPath:
    def __fspath__(self):
        return b"unsupported.json"


class FailingFile:
    def __init__(self, wrapped, operation):
        self.wrapped = wrapped
        self.operation = operation

    def write(self, value):
        if self.operation == "write":
            raise OSError("simulated write failure")
        return self.wrapped.write(value)

    def flush(self):
        if self.operation == "flush":
            raise OSError("simulated flush failure")
        return self.wrapped.flush()

    def __getattr__(self, name):
        return getattr(self.wrapped, name)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return self.wrapped.__exit__(exc_type, exc_value, traceback)


def write_json_pair(tmp_path, target_data=None, source_data=None):
    if target_data is None:
        target_data = {"target_secret": "preserved"}
    if source_data is None:
        source_data = {"source_secret": "preserved"}
    target = tmp_path / "target.json"
    source = tmp_path / "source.json"
    target.write_text(json.dumps(target_data), encoding="utf-8")
    source.write_text(json.dumps(source_data), encoding="utf-8")
    return target, source


def snapshot_files(*paths):
    return {
        path: path.read_bytes() if path.exists() else None
        for path in paths
    }


def assert_snapshot_unchanged(snapshot):
    for path, original_bytes in snapshot.items():
        if original_bytes is None:
            assert not path.exists()
        else:
            assert path.read_bytes() == original_bytes


def assert_detailed_failure(result, expected_reason, *paths):
    assert result == (False, expected_reason)
    reason = result[1]
    for path in paths:
        assert str(path) not in reason
    assert "target_secret" not in reason
    assert "source_secret" not in reason


def test_merge_json_files_combines_objects_and_preserves_source(tmp_path):
    target = tmp_path / "target.json"
    source = tmp_path / "source.json"
    target.write_text(
        json.dumps(
            {
                "AI": {"model": "target"},
                "SPL": {"smooth_checked": True},
            }
        ),
        encoding="utf-8",
    )
    source.write_text(
        json.dumps(
            {
                "Excel": {"enabled": True},
                "AI": {"model": "source"},
            }
        ),
        encoding="utf-8",
    )
    original_source_bytes = source.read_bytes()

    assert merge_json_files(target, source) is True
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "Excel": {"enabled": True},
        "AI": {"model": "target"},
        "SPL": {"smooth_checked": True},
    }
    assert source.read_bytes() == original_source_bytes


def test_merge_json_files_does_not_recursively_merge_nested_collision(tmp_path):
    target = tmp_path / "target.json"
    source = tmp_path / "source.json"
    target.write_text('{"AI": {"model": "target"}}', encoding="utf-8")
    source.write_text(
        '{"AI": {"model": "source", "missing_from_target": true}}',
        encoding="utf-8",
    )

    assert merge_json_files(target, source) is True
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "AI": {"model": "target"},
    }


def test_merge_json_files_with_reason_reports_ok(tmp_path):
    target, source = write_json_pair(tmp_path)

    assert merge_json_files_with_reason(target, source) == (True, "ok")


@pytest.mark.parametrize(
    "bad_path",
    [None, "", b"unsupported.json", 7, object(), EmptyPath(), BytesPath()],
    ids=[
        "none",
        "empty-string",
        "bytes",
        "integer",
        "object",
        "empty-pathlike",
        "bytes-pathlike",
    ],
)
@pytest.mark.parametrize("argument", ["target", "source"])
def test_invalid_path_arguments_are_rejected_without_changes(
    tmp_path,
    bad_path,
    argument,
):
    target, source = write_json_pair(tmp_path)
    original = snapshot_files(target, source)
    arguments = [target, source]
    arguments[0 if argument == "target" else 1] = bad_path

    result = merge_json_files_with_reason(*arguments)

    assert_detailed_failure(result, "invalid_path", target, source)
    assert_snapshot_unchanged(original)


@pytest.mark.parametrize("missing_argument", ["target", "source"])
def test_missing_files_are_rejected_without_changes(tmp_path, missing_argument):
    target, source = write_json_pair(tmp_path)
    missing = tmp_path / f"missing-{missing_argument}.json"
    arguments = [
        missing if missing_argument == "target" else target,
        missing if missing_argument == "source" else source,
    ]
    original = snapshot_files(target, source, missing)

    result = merge_json_files_with_reason(*arguments)

    assert_detailed_failure(result, "missing_file", target, source, missing)
    assert_snapshot_unchanged(original)


@pytest.mark.parametrize("directory_argument", ["target", "source"])
def test_directory_paths_are_rejected_as_missing_files(tmp_path, directory_argument):
    target, source = write_json_pair(tmp_path)
    directory = tmp_path / "directory"
    directory.mkdir()
    arguments = [
        directory if directory_argument == "target" else target,
        directory if directory_argument == "source" else source,
    ]
    original = snapshot_files(target, source)

    result = merge_json_files_with_reason(*arguments)

    assert_detailed_failure(result, "missing_file", target, source, directory)
    assert_snapshot_unchanged(original)


@pytest.mark.parametrize("unreadable_argument", ["target", "source"])
def test_permission_error_while_opening_either_file_leaves_both_unchanged(
    tmp_path,
    monkeypatch,
    unreadable_argument,
):
    target, source = write_json_pair(tmp_path)
    original = snapshot_files(target, source)
    unreadable_path = target if unreadable_argument == "target" else source
    real_open = builtins.open

    def permission_denied(path, *args, **kwargs):
        if os.path.abspath(os.fspath(path)) == os.path.abspath(unreadable_path):
            raise PermissionError("simulated read failure")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(json_merge, "open", permission_denied, raising=False)

    result = merge_json_files_with_reason(target, source)

    assert_detailed_failure(result, "unreadable_file", target, source)
    assert_snapshot_unchanged(original)


@pytest.mark.parametrize("undecodable_argument", ["target", "source"])
def test_invalid_utf8_is_an_unreadable_file_without_changes(
    tmp_path,
    undecodable_argument,
):
    target, source = write_json_pair(tmp_path)
    undecodable_path = target if undecodable_argument == "target" else source
    undecodable_path.write_bytes(b"\xff\xfe\xfa")
    original = snapshot_files(target, source)

    result = merge_json_files_with_reason(target, source)

    assert_detailed_failure(result, "unreadable_file", target, source)
    assert_snapshot_unchanged(original)


@pytest.mark.parametrize("invalid_argument", ["target", "source"])
def test_invalid_json_in_either_file_leaves_both_unchanged(
    tmp_path,
    invalid_argument,
):
    target, source = write_json_pair(tmp_path)
    invalid_path = target if invalid_argument == "target" else source
    invalid_path.write_text('{"target_secret": ', encoding="utf-8")
    original = snapshot_files(target, source)

    result = merge_json_files_with_reason(target, source)

    assert_detailed_failure(result, "invalid_json", target, source)
    assert_snapshot_unchanged(original)


@pytest.mark.parametrize("root", [[], 42, "scalar"], ids=["list", "number", "string"])
@pytest.mark.parametrize("invalid_argument", ["target", "source"])
def test_non_object_root_in_either_file_leaves_both_unchanged(
    tmp_path,
    root,
    invalid_argument,
):
    target, source = write_json_pair(tmp_path)
    invalid_path = target if invalid_argument == "target" else source
    invalid_path.write_text(json.dumps(root), encoding="utf-8")
    original = snapshot_files(target, source)

    result = merge_json_files_with_reason(target, source)

    assert_detailed_failure(result, "non_object_root", target, source)
    assert_snapshot_unchanged(original)


def test_same_lexical_path_is_rejected_without_changes(tmp_path):
    target = tmp_path / "target.json"
    target.write_text('{"target_secret": "preserved"}', encoding="utf-8")
    original = snapshot_files(target)

    result = merge_json_files_with_reason(target, target)

    assert_detailed_failure(result, "same_file", target)
    assert_snapshot_unchanged(original)


@pytest.mark.parametrize("alias_kind", ["hardlink", "symlink"])
def test_same_file_alias_is_rejected_without_changes(tmp_path, alias_kind):
    target = tmp_path / "target.json"
    alias = tmp_path / "alias.json"
    target.write_text('{"target_secret": "preserved"}', encoding="utf-8")
    try:
        if alias_kind == "hardlink":
            os.link(target, alias)
        else:
            alias.symlink_to(target)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"{alias_kind} is unavailable: {exc}")
    original = snapshot_files(target, alias)

    result = merge_json_files_with_reason(target, alias)

    assert_detailed_failure(result, "same_file", target, alias)
    assert_snapshot_unchanged(original)


def test_utf8_bom_input_is_accepted_and_output_has_no_bom(tmp_path):
    target = tmp_path / "target.json"
    source = tmp_path / "source.json"
    target.write_bytes(b"\xef\xbb\xbf" + '{"target": true}'.encode())
    source.write_bytes(b"\xef\xbb\xbf" + '{"source": true}'.encode())
    original_source_bytes = source.read_bytes()

    assert merge_json_files_with_reason(target, source) == (True, "ok")
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "source": True,
        "target": True,
    }
    assert not target.read_bytes().startswith(b"\xef\xbb\xbf")
    assert source.read_bytes() == original_source_bytes


def test_non_finite_numbers_follow_standard_json_behavior(tmp_path):
    target = tmp_path / "target.json"
    source = tmp_path / "source.json"
    target.write_text('{"not_a_number": NaN}', encoding="utf-8")
    source.write_text(
        '{"positive": Infinity, "negative": -Infinity}',
        encoding="utf-8",
    )

    assert merge_json_files_with_reason(target, source) == (True, "ok")
    merged = json.loads(target.read_text(encoding="utf-8"))
    assert math.isnan(merged["not_a_number"])
    assert merged["positive"] == math.inf
    assert merged["negative"] == -math.inf


def test_output_preserves_non_ascii_uses_six_spaces_and_source_first_order(
    tmp_path,
):
    target, source = write_json_pair(
        tmp_path,
        target_data={"共享": "目标值", "目标": "保留"},
        source_data={"新增": "补充值", "共享": "来源值"},
    )

    assert merge_json_files_with_reason(target, source) == (True, "ok")
    output_bytes = target.read_bytes()
    output_text = output_bytes.decode("utf-8")
    merged = json.loads(output_text)
    assert not output_bytes.startswith(b"\xef\xbb\xbf")
    assert "新增".encode("utf-8") in output_bytes
    assert b"\\u" not in output_bytes
    assert '\n      "新增"' in output_text
    assert '\n      "共享"' in output_text
    assert '\n      "目标"' in output_text
    assert list(merged) == ["新增", "共享", "目标"]
    assert merged["共享"] == "目标值"


def test_duplicate_members_follow_standard_last_value_behavior(tmp_path):
    target = tmp_path / "target.json"
    source = tmp_path / "source.json"
    target.write_text('{"duplicate": "first", "duplicate": "last"}', encoding="utf-8")
    source.write_text('{"source": true}', encoding="utf-8")

    assert merge_json_files_with_reason(target, source) == (True, "ok")
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "source": True,
        "duplicate": "last",
    }


def test_symbolic_link_target_entry_is_replaced_not_its_referent(tmp_path):
    referent = tmp_path / "referent.json"
    target = tmp_path / "target-link.json"
    source = tmp_path / "source.json"
    referent.write_text('{"target": "original"}', encoding="utf-8")
    source.write_text('{"source": "added"}', encoding="utf-8")
    original_referent_bytes = referent.read_bytes()
    try:
        target.symlink_to(referent)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symbolic links are unavailable: {exc}")

    assert merge_json_files_with_reason(target, source) == (True, "ok")
    assert not target.is_symlink()
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "source": "added",
        "target": "original",
    }
    assert referent.read_bytes() == original_referent_bytes


def test_hardlink_target_entry_is_replaced_not_its_other_alias(tmp_path):
    alias = tmp_path / "alias.json"
    target = tmp_path / "target-hardlink.json"
    source = tmp_path / "source.json"
    alias.write_text('{"target": "original"}', encoding="utf-8")
    source.write_text('{"source": "added"}', encoding="utf-8")
    original_alias_bytes = alias.read_bytes()
    try:
        os.link(alias, target)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"hardlinks are unavailable: {exc}")

    assert merge_json_files_with_reason(target, source) == (True, "ok")
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "source": "added",
        "target": "original",
    }
    assert alias.read_bytes() == original_alias_bytes
    assert not os.path.samefile(target, alias)


@pytest.mark.parametrize(
    "operation",
    ["create", "write", "flush", "fsync", "replace"],
)
def test_atomic_write_failures_leave_inputs_unchanged_and_remove_temp_files(
    tmp_path,
    monkeypatch,
    operation,
):
    target, source = write_json_pair(tmp_path)
    original = snapshot_files(target, source)
    original_directory_entries = {path.name for path in tmp_path.iterdir()}

    if operation == "create":
        def fail_create(*args, **kwargs):
            raise OSError("simulated temporary-file creation failure")

        monkeypatch.setattr(json_merge.tempfile, "mkstemp", fail_create)
    elif operation in {"write", "flush"}:
        real_fdopen = os.fdopen

        def failing_fdopen(*args, **kwargs):
            return FailingFile(real_fdopen(*args, **kwargs), operation)

        monkeypatch.setattr(json_merge.os, "fdopen", failing_fdopen)
    elif operation == "fsync":
        def fail_fsync(*args, **kwargs):
            raise OSError("simulated fsync failure")

        monkeypatch.setattr(json_merge.os, "fsync", fail_fsync)
    else:
        def fail_replace(*args, **kwargs):
            raise OSError("simulated replace failure")

        monkeypatch.setattr(json_merge.os, "replace", fail_replace)

    result = merge_json_files_with_reason(target, source)

    assert_detailed_failure(result, "write_failed", target, source)
    assert_snapshot_unchanged(original)
    assert {path.name for path in tmp_path.iterdir()} == original_directory_entries


@pytest.mark.parametrize(
    ("detailed_result", "expected"),
    [
        ((True, "ok"), True),
        ((False, "invalid_json"), False),
    ],
)
def test_boolean_wrapper_returns_only_detailed_success_item(
    monkeypatch,
    detailed_result,
    expected,
):
    monkeypatch.setattr(
        json_merge,
        "merge_json_files_with_reason",
        lambda *_args: detailed_result,
    )

    assert merge_json_files("target.json", "source.json") is expected
