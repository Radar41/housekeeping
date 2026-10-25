from housekeeping import cli, config, log


def test_log_line(tmp_path):
    p = tmp_path / "x.tsv"
    log.log_line(p, "test", "a", "b", "id1")
    assert p.read_text().count("\t") == 4
