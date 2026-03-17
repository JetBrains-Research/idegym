from pathlib import Path
from unittest import TestCase, main
from unittest.mock import mock_open, patch

from idegym.backend.utils.diff_patch import compute_diff
from idegym.tools.file_manager import FileManager


class TestFileManager(TestCase):
    def test_create_file(self):
        file_manager = FileManager()

        mock_content = "This is a test content."
        mock_file_path = Path("test_file.txt")

        with patch("builtins.open", mock_open()) as mocked_open:
            file_manager.create_file(mock_file_path, mock_content)
            mocked_open.assert_called_once_with(mock_file_path, "w", encoding="utf-8")
            mocked_open().write.assert_called_once_with(mock_content)

    def test_working_directory(self):
        mock_working_dir = Path("/mock/working/dir")
        mock_file_path = Path("test_file.txt")
        mock_content = "This is a test content."
        file_manager = FileManager(working_directory=mock_working_dir)

        with patch("builtins.open", mock_open()) as mocked_open:
            file_manager.create_file(mock_file_path, mock_content)
            mocked_open.assert_called_once_with(mock_working_dir / mock_file_path, "w", encoding="utf-8")
            mocked_open().write.assert_called_once_with(mock_content)

    def test_replace_lines(self):
        file_manager = FileManager()

        mock_file_path = Path("test_file.txt")
        mock_file_content = "Line 1\nLine 2\nLine 3\nLine 4\n"
        mock_new_content = "New Line 2 and 3"
        expected_content = "Line 1\nNew Line 2 and 3\nLine 4\n"

        with patch("builtins.open", mock_open(read_data=mock_file_content)) as mocked_open:
            file_manager.edit_file(mock_file_path, start_line=2, end_line=3, new_content=mock_new_content)
            mocked_open.assert_any_call(mock_file_path, "r", encoding="utf-8")
            mocked_open().readlines.assert_called_once()
            mocked_open.assert_any_call(mock_file_path, "w", encoding="utf-8")
            mocked_open().writelines.assert_called_once_with(expected_content.splitlines(keepends=True))

    def test_patch(self):
        old_content = "Hello, world!"
        patch_to_apply = compute_diff("Hello, world!", "Bonjour, world!")

        m = mock_open(read_data=old_content)
        path = Path("/tmp/test.txt")
        with patch("builtins.open", m):
            FileManager().patch_file(path, patch_to_apply)

            m.assert_any_call(path, "r", encoding="utf-8")
            m().write.assert_any_call("Bonjour, world!")


if __name__ == "__main__":
    main()
