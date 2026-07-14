"""Headless tests for the phone-pairing helpers (ferrodac.ui.phonepair).

lan_ip is pure stdlib (no Qt). qr_pixmap / PairPhoneDialog need an offscreen QGuiApp
and segno; both are guarded so the suite skips cleanly where they are absent.
"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def test_lan_ip_returns_str():
    from ferrodac.ui.phonepair import lan_ip
    ip = lan_ip()
    assert isinstance(ip, str) and ip


@pytest.mark.ui
def test_qr_pixmap_non_null():
    pytest.importorskip("segno")
    from qtpy.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from ferrodac.ui.phonepair import qr_pixmap
    pm = qr_pixmap("http://192.168.1.20:8000/enter?k=abc123")
    assert not pm.isNull()
    assert pm.width() > 0 and pm.height() > 0


@pytest.mark.ui
def test_dialog_regenerate_updates_url():
    from qtpy.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from ferrodac.ui.phonepair import PairPhoneDialog
    dlg = PairPhoneDialog(None, url="http://192.168.1.20:8000", psk="old",
                          on_regenerate=lambda: "new", on_revoke=lambda: None)
    assert dlg._url.text() == "http://192.168.1.20:8000/enter?k=old"
    dlg._regenerate()
    assert dlg._url.text() == "http://192.168.1.20:8000/enter?k=new"
