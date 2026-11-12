"""The code-card check: facts on the card against the inventory and the disk, no model."""
from pathlib import Path

from housekeeping.tasks.kinds import code_card

from tasks_fixtures import GOOD_CLAIMS


def setup(settings, **claims):
    root = str(Path(settings.inventory_dir).parent / "u")
    inp = code_card.input_from_row({"root_id": "21", "root_path": root, "dir": "fit", "exts": "py"})
    card = {"claims": dict(GOOD_CLAIMS, **claims)}
    return card, inp, root


def run(settings, card, inp):
    return code_card.check(card, inp, inventory_dir=settings.inventory_dir)


def test_good_card_passes(settings):
    card, inp, _ = setup(settings)
    ok, detail, retry = run(settings, card, inp)
    assert ok and retry, detail


def test_folder_relative_citation_accepted(settings):
    card, inp, _ = setup(settings, entry_point="run.py", files_cited=["./util.py"])
    assert run(settings, card, inp)[0]


def test_made_up_file_fails_and_may_retry(settings):
    card, inp, _ = setup(settings, files_cited=["fit/run.py", "fit/pyproject.toml"])
    ok, detail, retry = run(settings, card, inp)
    assert not ok and retry and "fit/pyproject.toml" in detail


def test_folder_name_as_entry_point_fails(settings):
    card, inp, _ = setup(settings, entry_point="fit")
    assert not run(settings, card, inp)[0]


def test_other_folder_or_escape_or_protected_fails(settings):
    for bad in ("plot/p.m", "../fit/run.py", "fit/protected/key.py"):
        card, inp, _ = setup(settings, files_cited=[bad])
        ok, detail, _ = run(settings, card, inp)
        assert not ok, bad


def test_changed_file_is_stale_no_retry(settings):
    card, inp, root = setup(settings)
    Path(root, "fit/run.py").write_text("edited\n")
    ok, detail, retry = run(settings, card, inp)
    assert not ok and not retry and "changed since inventory" in detail


def test_deleted_file_and_moved_folder_no_retry(settings):
    card, inp, root = setup(settings)
    Path(root, "fit/run.py").unlink()
    ok, detail, retry = run(settings, card, inp)
    assert not ok and not retry and "missing on disk" in detail
    inp2 = dict(inp, root_path=root + "-renamed")
    ok, detail, retry = run(settings, card, inp2)
    assert not ok and not retry


def test_language_must_fit_extensions(settings):
    card, inp, _ = setup(settings, language="Rust")
    ok, detail, _ = run(settings, card, inp)
    assert not ok and "language rust" in detail
    card, inp, _ = setup(settings, language="Fortran")
    ok, detail, _ = run(settings, card, inp)
    assert ok and "not checked" in detail


def test_no_citations_fails(settings):
    card, inp, _ = setup(settings, entry_point="", files_cited=[])
    assert not run(settings, card, inp)[0]


def test_stale_task_is_not_retried_and_needs_you(store, settings, client, names, capsys):
    from housekeeping.tasks.cli import stuck
    from housekeeping.tasks.dispatch import run_once
    from housekeeping.tasks.model import Task

    _, inp, root = setup(settings)
    Path(root, "fit/run.py").write_text("edited\n")
    t = Task.new("code-card", inp, 2)
    store.propose(t)
    run_once(store, settings, apply=True, protected_names=names, client=client, out=lambda *_: None)
    assert store.get(t.id).status == "failed"
    counts = run_once(store, settings, apply=True, protected_names=names, client=client, out=lambda *_: None)
    assert counts == {} and store.get(t.id).attempts == 1
    assert [x.id for x, _ in stuck(store)] == [t.id]
