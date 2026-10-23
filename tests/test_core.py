import json
import threading

import pytest
import requests

from requiem_archive.core import ArchiveClient, Capture, Cancelled, Recovery, local_path, normalize, rewrite


@pytest.mark.parametrize('url', ['file:///etc/passwd', 'http://localhost/', 'http://127.0.0.1/', 'http://user:pass@example.com', 'https://example.com:8080/'])
def test_reject_bad_urls(url):
    with pytest.raises(ValueError):
        normalize(url)


def test_paths_are_safe_and_unique():
    urls = ['https://a.test/../../CON', 'https://a.test/x?q=1', 'https://a.test/x?q=2', 'https://a.test/X']
    paths = [local_path(u) for u in urls]
    assert len(set(paths)) == 4
    assert all(p.startswith('files/') and '..' not in p for p in paths)


class FakeClient:
    def check(self):
        pass

    def fetch(self, url, stamp):
        if url.endswith('missing.png'):
            raise requests.HTTPError('404')
        if url.endswith('style.css'):
            return b'body{background:url(missing.png)}', 'text/css', 'https://web.archive.org/replay'
        return b'<html><head><link rel="stylesheet" href="/style.css"></head><body><script>alert(1)</script><a href="/next">Next</a><img src="https://evil.test/x"><a href="javascript:alert(1)">x</a></body></html>', 'text/html', 'https://web.archive.org/replay'


def test_recovery_integration_partial_and_rewrite(tmp_path):
    report = Recovery(FakeClient()).run('https://a.test/', '20200101000000', tmp_path, 4)
    assert report['status'] == 'partial'
    assert report['limited'] is False
    html = (tmp_path / local_path('https://a.test/')).read_text('utf-8')
    assert '<script' not in html and 'javascript:' not in html and 'https://evil.test' not in html
    assert local_path('https://a.test/style.css').split('/')[1] in html
    assert (tmp_path / local_path('https://a.test/missing.png')).exists()
    assert json.loads((tmp_path / 'report.json').read_text('utf-8')) == report
    assert (tmp_path / 'index.html').exists()


def test_limit_and_nonempty_directory(tmp_path):
    report = Recovery(FakeClient()).run('https://a.test/', '20200101000000', tmp_path, 1)
    assert report['limited'] and len(report['files']) == 1
    with pytest.raises(ValueError, match='empty'):
        Recovery(FakeClient()).run('https://a.test/', '20200101000000', tmp_path)


def test_cancel_preserves_report(tmp_path):
    class Client(FakeClient):
        def check(self):
            raise Cancelled()
    report = Recovery(Client()).run('https://a.test/', '20200101000000', tmp_path)
    assert report['status'] == 'cancelled'
    assert (tmp_path / 'report.json').exists()


def test_archive_client_blocks_live_redirect():
    class Response:
        status_code = 302
        is_redirect = True
        url = 'https://web.archive.org/web/20200101000000id_/http://a.test/'
        headers = {'Location': 'http://127.0.0.1/secret'}
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    class Session:
        headers = {}
        def get(self, *args, **kwargs):
            return Response()
    with pytest.raises(ValueError, match='Blocked'):
        ArchiveClient(session=Session()).fetch('http://a.test/', '20200101000000')


def test_cancellation_before_request():
    event = threading.Event()
    event.set()
    with pytest.raises(Cancelled):
        ArchiveClient(event).timeline('https://example.com/')


def test_timeline_parses_empty_and_valid():
    client = ArchiveClient()
    client.get = lambda *a, **k: (b'[]', '', '')
    assert client.timeline('https://example.com') == []
    client.get = lambda *a, **k: (json.dumps([['timestamp', 'original', 'mimetype'], ['20200101000000', 'https://example.com/', 'text/html']]).encode(), '', '')
    assert client.timeline('https://example.com') == [Capture('20200101000000', 'https://example.com/', 'text/html')]


def test_css_import_and_base():
    links = []
    def resolve(raw, base):
        links.append((raw, base))
        return 'local.css'
    html = rewrite(b'<base href="https://a.test/sub/"><img src="a.png"><style>@import "a.css";</style>', 'text/html', 'https://a.test/', resolve)
    assert ('a.png', 'https://a.test/sub/') in links
    assert b'@import "local.css"' in html

