import pytest

import mboxlib
from conftest import make_message, write_mbox


@pytest.mark.parametrize("flavour", ["gmail", "thunderbird", "generic"])
def test_each_separator_flavour_is_detected_and_split(tmp_path, flavour):
    p = tmp_path / "box.mbox"
    offsets = write_mbox(p, [make_message(i) for i in range(3)], flavour=flavour)
    assert mboxlib.detect_flavour(p) == (flavour, 3)
    size, last, _ = mboxlib.find_last_boundary(p)
    got = [off for off, _ in mboxlib.iter_messages(p, size)]
    assert got == offsets
    assert last == offsets[-1]


def test_body_line_starting_with_from_is_not_a_separator(tmp_path):
    p = tmp_path / "box.mbox"
    write_mbox(p, [make_message(1, body="From the desk of nobody\nFrom here on"), make_message(2)])
    size, _, _ = mboxlib.find_last_boundary(p)
    assert len(list(mboxlib.iter_messages(p, size))) == 2


def test_messages_round_trip_byte_for_byte(tmp_path):
    p = tmp_path / "box.mbox"
    write_mbox(p, [make_message(i) for i in range(4)])
    data = p.read_bytes()
    size, _, _ = mboxlib.find_last_boundary(p)
    assert b"".join(raw for _, raw in mboxlib.iter_messages(p, size)) == data


def test_truncated_download_boundary(tmp_path):
    p = tmp_path / "box.mbox"
    offsets = write_mbox(p, [make_message(i) for i in range(3)])
    with open(p, "ab") as f:
        f.write(b"From 1700000000000000999@xxx Mon Mar 03 10:00:00 +0000 2025\nSubject: cut off mid-mess")
    size, last, _ = mboxlib.find_last_boundary(p)
    assert last > offsets[-1] and last < size


def test_discover_takeout_and_thunderbird_profile(tmp_path):
    write_mbox(tmp_path / "Takeout" / "Mail" / "Inbox.mbox", [make_message(1)])
    prof = tmp_path / "profile" / "Mail" / "Local Folders"
    write_mbox(prof / "INBOX", [make_message(2)], flavour="thunderbird")
    (prof / "INBOX.msf").write_text("index")
    write_mbox(prof / "INBOX.sbd" / "Projects", [make_message(3)], flavour="thunderbird")
    (prof / "notes.txt").write_text("not mail")
    found = [label.replace("\\", "/") for label, _ in mboxlib.discover(tmp_path)]
    assert found == sorted(["Takeout/Mail/Inbox.mbox", "profile/Mail/Local Folders/INBOX",
                            "profile/Mail/Local Folders/INBOX.sbd/Projects"])


def test_discover_single_file(tmp_path):
    p = tmp_path / "one.mbox"
    write_mbox(p, [make_message(1)])
    assert mboxlib.discover(p) == [("one.mbox", str(p))]


def test_version_is_semver():
    parts = mboxlib.__version__.split(".")
    assert len(parts) == 3 and all(x.isdigit() for x in parts)
