import unittest
from unittest.mock import patch, MagicMock
import os
import subprocess
from multi_map_exporter import sanitize_filename, check_ssh_connection, create_remote_directory, transfer_files_scp

class TestMultiMapExporter(unittest.TestCase):

    # def test_sanitize_filename(self):
    #     self.assertEqual(sanitize_filename("test file.png"), "test_file.png")
    #     self.assertEqual(sanitize_filename("map!@#$%^&*.yaml"), "map_________.yaml")

    @patch('subprocess.run')
    def test_check_ssh_connection(self, mock_run):
        mock_run.return_value.returncode = 0
        self.assertTrue(check_ssh_connection("password"))
        
        mock_run.side_effect = subprocess.CalledProcessError(1, "cmd")
        self.assertFalse(check_ssh_connection("password"))

    @patch('subprocess.run')
    def test_create_remote_directory(self, mock_run):
        create_remote_directory("robot_123", "password")
        mock_run.assert_called_once()

    @patch('subprocess.run')
    def test_transfer_files_scp(self, mock_run):
        transfer_files_scp("local_file.png", "robot_123", "/remote/dir/", "password")
        mock_run.assert_called_once()

if __name__ == '__main__':
    unittest.main()
