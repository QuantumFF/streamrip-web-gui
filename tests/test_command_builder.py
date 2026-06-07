"""Seam 2: the rip command builder is a pure function."""
import app as app_module
from app import build_rip_command, build_search_command


def test_command_includes_quality_url_and_download_dir():
    cmd = build_rip_command(
        'https://qobuz.com/album/123', 3,
        config_path=None, download_dir='/music',
    )
    assert cmd[0] == 'rip'
    assert '-q' in cmd and cmd[cmd.index('-q') + 1] == '3'
    assert '-f' in cmd and cmd[cmd.index('-f') + 1] == '/music'
    assert cmd[-2:] == ['url', 'https://qobuz.com/album/123']


def test_command_omits_config_path_when_missing(tmp_path):
    missing = str(tmp_path / 'nope.toml')
    cmd = build_rip_command('https://x/1', 2, config_path=missing, download_dir='/m')
    assert '--config-path' not in cmd


def test_command_includes_config_path_when_present(tmp_path):
    cfg = tmp_path / 'config.toml'
    cfg.write_text('x = 1')
    cmd = build_rip_command('https://x/1', 2, config_path=str(cfg), download_dir='/m')
    assert '--config-path' in cmd
    assert cmd[cmd.index('--config-path') + 1] == str(cfg)


def test_redownload_adds_no_db_flag():
    cmd = build_rip_command('https://x/1', 3, download_dir='/m', no_db=True)
    assert '--no-db' in cmd


def test_normal_download_omits_no_db_flag():
    cmd = build_rip_command('https://x/1', 3, download_dir='/m')
    assert '--no-db' not in cmd


def test_search_command_orders_source_type_query():
    cmd = build_search_command('qobuz', 'album', 'daft punk', '/tmp/out.txt')
    assert cmd[0] == 'rip'
    assert '--output-file' in cmd
    assert cmd[cmd.index('--output-file') + 1] == '/tmp/out.txt'
    assert cmd[-3:] == ['qobuz', 'album', 'daft punk']


def test_search_command_omits_config_path_when_missing(tmp_path):
    missing = str(tmp_path / 'nope.toml')
    cmd = build_search_command(
        'tidal', 'track', 'q', '/tmp/o.txt', config_path=missing
    )
    assert '--config-path' not in cmd


def test_search_command_includes_config_path_when_present(tmp_path):
    cfg = tmp_path / 'config.toml'
    cfg.write_text('x = 1')
    cmd = build_search_command(
        'tidal', 'track', 'q', '/tmp/o.txt', config_path=str(cfg)
    )
    assert '--config-path' in cmd
    assert cmd[cmd.index('--config-path') + 1] == str(cfg)
