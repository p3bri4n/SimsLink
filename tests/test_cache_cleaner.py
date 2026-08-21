from backend import cache_cleaner


def _touch_file(path, content=b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def test_list_cache_targets_reports_existence(app_config):
    _touch_file(app_config.sims4_user_dir / "localthumbcache.package")

    targets = cache_cleaner.list_cache_targets(app_config)

    by_name = {t.name: t for t in targets}
    assert by_name["localthumbcache.package"].exists is True
    assert by_name["cache"].exists is False


def test_clean_cache_deletes_known_files(app_config):
    _touch_file(app_config.sims4_user_dir / "localthumbcache.package")
    _touch_file(app_config.sims4_user_dir / "localsimtexturecache.package")

    cleaned = cache_cleaner.clean_cache(app_config)

    assert set(cleaned) >= {"localthumbcache.package", "localsimtexturecache.package"}
    assert not (app_config.sims4_user_dir / "localthumbcache.package").exists()
    assert not (app_config.sims4_user_dir / "localsimtexturecache.package").exists()


def test_clean_cache_clears_contents_but_keeps_folder_and_filecache_cfg(app_config):
    cache_dir = app_config.sims4_user_dir / "cache"
    _touch_file(cache_dir / "somefile.tmp")
    _touch_file(cache_dir / "FileCache.cfg")

    cache_cleaner.clean_cache(app_config)

    assert cache_dir.is_dir()
    assert not (cache_dir / "somefile.tmp").exists()
    assert (cache_dir / "FileCache.cfg").exists()


def test_clean_cache_deletes_cachewebkit_and_onlinethumbnailcache_dirs(app_config):
    _touch_file(app_config.sims4_user_dir / "cachewebkit" / "x.tmp")
    _touch_file(app_config.sims4_user_dir / "onlinethumbnailcache" / "y.tmp")

    cache_cleaner.clean_cache(app_config)

    assert not (app_config.sims4_user_dir / "cachewebkit").exists()
    assert not (app_config.sims4_user_dir / "onlinethumbnailcache").exists()


def test_clean_cache_never_touches_protected_files(app_config):
    _touch_file(app_config.sims4_user_dir / "options.ini")
    _touch_file(app_config.sims4_user_dir / "resource.cfg")
    _touch_file(app_config.sims4_user_dir / "lastException.txt")
    _touch_file(app_config.sims4_user_dir / "lastCrash.txt")
    _touch_file(app_config.sims4_user_dir / "saves" / "save1.save")
    _touch_file(app_config.sims4_user_dir / "Tray" / "household.trayitem")
    _touch_file(app_config.sims4_user_dir / "Screenshots" / "shot.png")

    cache_cleaner.clean_cache(app_config)

    assert (app_config.sims4_user_dir / "options.ini").exists()
    assert (app_config.sims4_user_dir / "resource.cfg").exists()
    assert (app_config.sims4_user_dir / "lastException.txt").exists()
    assert (app_config.sims4_user_dir / "lastCrash.txt").exists()
    assert (app_config.sims4_user_dir / "saves" / "save1.save").exists()
    assert (app_config.sims4_user_dir / "Tray" / "household.trayitem").exists()
    assert (app_config.sims4_user_dir / "Screenshots" / "shot.png").exists()


def test_clean_cache_is_noop_when_nothing_present(app_config):
    cleaned = cache_cleaner.clean_cache(app_config)

    assert cleaned == []
