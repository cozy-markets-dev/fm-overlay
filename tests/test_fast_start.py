import pytest

from tools.build_fast_start import runtime_path


@pytest.mark.parametrize('name', ['../outside.dll', r'a\..\outside.dll', r'C:\outside.dll',
                                 r'\outside.dll', r'\\server\share\outside.dll',
                                 'file:stream', 'folder.\\file.dll', 'folder \\file.dll', ''])
def test_runtime_archive_cannot_write_outside_its_folder(tmp_path, name):
    with pytest.raises(ValueError):
        runtime_path(tmp_path, name)
    assert list(tmp_path.iterdir()) == []


def test_windows_runtime_paths_work_on_the_build_machine(tmp_path):
    assert runtime_path(tmp_path, r'cv2\cv2.pyd') == tmp_path / 'cv2' / 'cv2.pyd'
    assert runtime_path(tmp_path, 'mprice_decoder/assets/private-upload.json') == (
        tmp_path / 'mprice_decoder' / 'assets' / 'private-upload.json')
