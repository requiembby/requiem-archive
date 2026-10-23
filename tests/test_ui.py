import os
import time

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from requiem_archive.ui import Window, create_application


def test_window_states_and_empty_timeline():
    app = create_application()
    window = Window()
    assert window.find.isEnabled()
    assert not window.stop.isEnabled()
    window.show_captures([])
    assert window.timeline.count() == 0
    window.download()
    assert 'select' in window.log.toPlainText()
    window.show()
    from requiem_archive.core import Capture
    def wait_for_worker():
        deadline = time.monotonic() + 5
        while not window.find.isEnabled() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert window.find.isEnabled()
        assert not window.worker.isRunning()
    for _ in range(3):
        window.start(lambda client, log: [Capture('20200101000000', 'https://kun.uz/', 'text/html')], window.show_captures)
        wait_for_worker()
        assert window.timeline.count() >= 1
    def failure(client, log):
        raise RuntimeError('Wayback unavailable')
    window.start(failure, window.show_captures)
    wait_for_worker()
    assert 'Wayback unavailable' in window.log.toPlainText()
    assert window.isVisible()
    window.close()
    app.processEvents()
