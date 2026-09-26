# Smoke test: the package must import cleanly without a board build.


def test_top_level_package_imports():
    import annealage_pod

    assert isinstance(annealage_pod.__version__, str)
