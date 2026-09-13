from unittest.mock import MagicMock, patch

from server.run import main


def test_server_run_main():
    with patch("uvicorn.Config") as mock_config_cls, patch("uvicorn.Server") as mock_server_cls:
        mock_server = MagicMock()
        mock_server_cls.return_value = mock_server

        main()

        mock_config_cls.assert_called_once()
        mock_server_cls.assert_called_once()
        mock_server.run.assert_called_once()
